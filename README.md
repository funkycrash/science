# Brief.science archive

A static, read-only copy of the Brief.science articles and editions, for personal reference.

The site is plain HTML in `site/`. Netlify publishes that directory as is. There is no build step.

## Pages

- `/`: latest articles, themes, latest editions.
- `/articles/`: every article, by year.
- `/article/<slug>/`: one article, with related articles and previous/next links.
- `/editions/` and `/edition/<slug>/`: the weekly editions, full text.
- `/thematiques/` and `/thematique/<slug>/`: articles by theme.
- `/recherche/`: client-side search over `search.json`.

## Styling

- `site/assets/css/style.css`: the stylesheet.
- `site/assets/css/fonts.css` and `site/assets/fonts/`: self-hosted Source Sans 3 and Source Serif 4.
- `site/assets/js/search.js`: the search script.

## Access

`netlify/edge-functions/auth.ts` protects every path with HTTP Basic Auth.

- Username: `funkycrash`
- Password: the value of the `SITE_PASSWORD` environment variable

Set `SITE_PASSWORD` in Netlify under Project configuration > Environment variables. If the variable is missing, the site answers 503.
