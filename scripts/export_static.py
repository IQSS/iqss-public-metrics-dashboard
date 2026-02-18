#!/usr/bin/env python3
"""
Export a static snapshot of a Glance dashboard for GitHub Pages.

Glance pages are an SPA shell; real page content is fetched from:
  /api/pages/<slug>/content/

This script:
  - fetches the shell HTML for each page
  - fetches rendered content HTML for each page
  - injects content into the shell
  - marks the page as "content-ready" so the loader is hidden (CSS-driven)
  - downloads Glance static assets needed for rendering (bundle.css + referenced fonts/images)
  - copies repo assets/ (images/fonts/json/css)
  - rewrites root-absolute links (/assets, /static, /overview, ...) to include a base path
    suitable for GitHub project pages (e.g. "/<repo-name>")
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def _fetch_bytes(url: str, timeout_s: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "iqss-glance-static-export/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _fetch_text(url: str, timeout_s: float = 20.0) -> str:
    return _fetch_bytes(url, timeout_s=timeout_s).decode("utf-8", errors="replace")


def _mkdirp(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_bytes(path: Path, data: bytes) -> None:
    _mkdirp(path.parent)
    path.write_bytes(data)


def _write_text(path: Path, data: str) -> None:
    _mkdirp(path.parent)
    path.write_text(data, encoding="utf-8")


def _normalize_base_path(base_path: str) -> str:
    """
    "" (empty) means no rewrite (useful for local preview at /).
    "/repo" is the GitHub Pages project base path.
    """
    base_path = base_path.strip()
    if base_path in ("", "/"):
        return ""
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    return base_path.rstrip("/")


def _discover_slugs(config_dir: Path) -> list[str]:
    # Keep this YAML-free: we just regex for `slug: <value>` in config/*.yml.
    slug_re = re.compile(r"^\s*slug:\s*([A-Za-z0-9_-]+)\s*$")
    slugs: list[str] = []
    seen: set[str] = set()

    for yml in sorted(config_dir.glob("*.yml")):
        try:
            for line in yml.read_text(encoding="utf-8", errors="replace").splitlines():
                m = slug_re.match(line)
                if not m:
                    continue
                slug = m.group(1)
                if slug in seen:
                    continue
                seen.add(slug)
                slugs.append(slug)
        except FileNotFoundError:
            continue

    # Prefer Home first for predictable output.
    if "home" in seen:
        slugs = ["home"] + [s for s in slugs if s != "home"]
    return slugs


def _extract_bundle_css_path(shell_html: str) -> str:
    # Example: <link rel="stylesheet" href='/static/<hash>/css/bundle.css'>
    m = re.search(
        r"<link[^>]+href=['\"](/static/[^'\"]+/css/bundle\.css)['\"][^>]*>",
        shell_html,
        flags=re.IGNORECASE,
    )
    if not m:
        raise RuntimeError("Could not find bundle.css path in page HTML")
    return m.group(1)


def _extract_page_js_path(shell_html: str) -> str | None:
    m = re.search(
        r"<script[^>]+src=['\"](/static/[^'\"]+/js/page\.js)['\"][^>]*></script>",
        shell_html,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def _inject_content(shell_html: str, content_html: str) -> str:
    # 1) Inject content into the placeholder.
    # The shell contains:
    #   <div class="page-content" id="page-content"></div>
    injected, n = re.subn(
        r'(<div[^>]*\bid=["\']page-content["\'][^>]*>)\s*</div>',
        r"\1" + content_html + r"</div>",
        shell_html,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if n != 1:
        raise RuntimeError("Failed to inject page content (page-content div not found)")

    # 2) Mark as content-ready so Glance CSS shows content and hides loader.
    # <main class="page" ... aria-busy="true">
    # -> <main class="page content-ready" ... aria-busy="false">
    injected = re.sub(
        r'(<main[^>]*\bclass=["\'])page(\b[^"\']*["\'][^>]*>)',
        r"\1page content-ready\2",
        injected,
        count=1,
        flags=re.IGNORECASE,
    )
    injected = re.sub(
        r'(\baria-busy=["\'])true(["\'])',
        r"\1false\2",
        injected,
        count=1,
        flags=re.IGNORECASE,
    )

    # 3) Remove the SPA JS boot file so it doesn't try to re-fetch /api at runtime.
    injected = re.sub(
        r"<script[^>]+src=['\"]/static/[^'\"]+/js/page\.js['\"][^>]*></script>\s*",
        "",
        injected,
        count=1,
        flags=re.IGNORECASE,
    )
    return injected


def _rewrite_base_paths(text: str, base_path: str) -> str:
    """
    Prefix root-absolute paths with base_path:
      href="/assets/.." -> href="/<base>/assets/.."
    Avoid protocol-relative URLs like href="//example.com".
    """
    if not base_path:
        return text

    # Common HTML attributes with root-absolute URLs.
    for attr in ("href", "src", "action"):
        text = re.sub(
            rf'{attr}="/(?!/)',
            f'{attr}="{base_path}/',
            text,
        )
        text = re.sub(
            rf"{attr}='/(?!/)",
            f"{attr}='{base_path}/",
            text,
        )

    # CSS url() root-absolute URLs.
    text = re.sub(r"url\('/(?!/)", f"url('{base_path}/", text)
    text = re.sub(r'url\("/(?!/)', f'url("{base_path}/', text)

    # Glance uses a relative manifest href (manifest.json) which breaks on /<slug>/ pages.
    # Make it base-absolute.
    text = re.sub(
        r"""href=(['"])manifest\.json""",
        rf"href=\1{base_path}/manifest.json",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _download_static_css_and_deps(glance_url: str, out_dir: Path, bundle_css_path: str) -> None:
    css_url = urllib.parse.urljoin(glance_url.rstrip("/") + "/", bundle_css_path.lstrip("/"))
    css_bytes = _fetch_bytes(css_url)
    css_out = out_dir / bundle_css_path.lstrip("/")
    _write_bytes(css_out, css_bytes)

    css_text = css_bytes.decode("utf-8", errors="replace")
    css_dir = "/" + str(Path(bundle_css_path).parent).lstrip("/")

    # Extract url(...) references. This intentionally ignores @import (not expected here).
    # Handles url(foo), url('foo'), url("foo").
    url_re = re.compile(r"url\(\s*(['\"]?)([^'\"\)]+)\1\s*\)")
    refs: set[str] = set()

    for m in url_re.finditer(css_text):
        ref = m.group(2).strip()
        if not ref or ref.startswith("data:"):
            continue
        if ref.startswith("http://") or ref.startswith("https://"):
            continue

        if ref.startswith("/"):
            refs.add(ref)
            continue

        # Resolve relative to the CSS directory.
        resolved = posixpath.normpath(posixpath.join(css_dir, ref))
        if not resolved.startswith("/"):
            resolved = "/" + resolved
        refs.add(resolved)

    for ref in sorted(refs):
        ref_url = urllib.parse.urljoin(glance_url.rstrip("/") + "/", ref.lstrip("/"))
        try:
            data = _fetch_bytes(ref_url)
        except Exception as e:
            raise RuntimeError(f"Failed to download static dependency {ref} from {ref_url}: {e}") from e
        _write_bytes(out_dir / ref.lstrip("/"), data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--glance-url", default=os.environ.get("GLANCE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--out-dir", default=os.environ.get("OUT_DIR", "dist"))
    parser.add_argument("--base-path", default=os.environ.get("BASE_PATH", ""))
    parser.add_argument("--config-dir", default=os.environ.get("CONFIG_DIR", "config"))
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()

    glance_url = args.glance_url.rstrip("/")
    out_dir = Path(args.out_dir)
    base_path = _normalize_base_path(args.base_path)
    config_dir = Path(args.config_dir)

    slugs = _discover_slugs(config_dir)
    if not slugs:
        print(f"ERROR: No page slugs found under {config_dir}/", file=sys.stderr)
        return 2

    # Wait for Glance to be up (use /home if present, else /).
    start = time.time()
    probe_path = "/home" if "home" in slugs else "/"
    while True:
        try:
            _fetch_bytes(glance_url + probe_path, timeout_s=5.0)
            break
        except Exception:
            if time.time() - start > args.timeout_seconds:
                print(f"ERROR: Glance did not become ready at {glance_url} within timeout", file=sys.stderr)
                return 3
            time.sleep(0.5)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    _mkdirp(out_dir)

    # Copy repo assets/ as-is (data, fonts, images, custom CSS).
    repo_assets = Path("assets")
    if not repo_assets.is_dir():
        print("ERROR: assets/ directory not found in repo root", file=sys.stderr)
        return 4
    shutil.copytree(repo_assets, out_dir / "assets")

    # Fetch manifest.json (used by Glance shell).
    try:
        manifest = _fetch_bytes(glance_url + "/manifest.json")
        _write_bytes(out_dir / "manifest.json", manifest)
    except Exception:
        # Not fatal for static rendering.
        pass

    # Use one shell page to find Glance's static bundle CSS path.
    sample_shell = _fetch_text(glance_url + ("/home" if "home" in slugs else f"/{slugs[0]}"))
    bundle_css_path = _extract_bundle_css_path(sample_shell)
    _download_static_css_and_deps(glance_url, out_dir, bundle_css_path)

    # Build each page.
    for slug in slugs:
        shell_html = _fetch_text(glance_url + f"/{slug}")
        content_html = _fetch_text(glance_url + f"/api/pages/{slug}/content/")
        page_html = _inject_content(shell_html, content_html)
        _write_text(out_dir / slug / "index.html", page_html)

        if slug == "home":
            _write_text(out_dir / "index.html", page_html)

    # Rewrite base paths in exported HTML/CSS (notably assets/user.css contains /assets/... URLs).
    for p in out_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".html", ".css"):
            continue
        try:
            original = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rewritten = _rewrite_base_paths(original, base_path)
        if rewritten != original:
            p.write_text(rewritten, encoding="utf-8")

    # GitHub Pages: ensure Jekyll is disabled.
    _write_text(out_dir / ".nojekyll", "")

    print(f"Exported {len(slugs)} pages to {out_dir}/ (base path: {base_path or '(none)'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
