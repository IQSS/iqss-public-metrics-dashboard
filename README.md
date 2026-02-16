# IQSS Public Metrics Dashboard (Glance Example)

This repository is an **example** rebuild of the legacy IQSS public metrics dashboard using
[Glance](https://github.com/glanceapp/glance).

The look-and-feel (colors/fonts/images) is inspired by the legacy site:
https://iqss.github.io/iqss-metrics-dashboard/

All metrics are **fake** and served as **static JSON files** so you can compare Glance against
the existing dashboard implementation.

## Run

Requirements:
- Glance installed (or downloaded)

Start Glance from the repo root:
```bash
glance --config ./config/glance.yml
```

Open:
- Dashboard: http://localhost:8080

## Where Things Live

- Glance config: `config/glance.yml` and `config/*.yml`
- Custom styling (IQSS-inspired): `assets/user.css`
- Images (copied from the legacy dashboard repo): `assets/images/`
- Fake static data (flat files served by Glance): `assets/data/*.json`
- No-JS graph examples (line/area/bar/pie): `assets/data/graphs/*.json`

## Offline / No Runtime Internet Calls

This example uses no custom JS for graphs. Charts are rendered as
HTML/CSS/SVG from static JSON using Glance templates.

This example self-hosts its font dependencies:
- Fonts: `assets/fonts/*.woff2` (Montserrat + Questrial)

To (re)download vendor assets:
```bash
./scripts/vendor-assets.sh
```

## Reusable Graph Template (No JS)

Graph widgets use a shared Glance template in
`config/scientific-programs.yml` (anchor: `&iqss_graph_template`) and
accept these JSON shapes:

- `type: "line"` or `type: "area"` with `points: [{label, value}, ...]`
- `type: "bar"` with `bars: [{label, value}, ...]`
- `type: "pie"` with `slices: [{label, value, color}, ...]`

To add another graph, point a `custom-api` widget URL to a static graph JSON
file and set `template: *iqss_graph_template`.
