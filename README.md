# Brief.science archive

A static, read-only copy of the Brief.science articles and editions, for personal reference.

The site is plain HTML in `site/`. Netlify publishes that directory as is. There is no build step on Netlify.

## Layout

- `build.py`: reads the SiteSucker dump and writes `site/`.
- `assets/`: stylesheet, self-hosted fonts, search script, favicon. Copied into `site/assets/` at build time.
- `site/`: the generated site. Committed, because the source dump is not in the repository.

## Rebuild

The dump must be at `~/Downloads/us.sitesucker.mac.sitesucker`, or pass `--src`.

```
python3 -m venv venv
./venv/bin/pip install beautifulsoup4 pillow
./venv/bin/python build.py
```

The script deletes `site/` and writes it again. Then commit the result.

## Pages

- `/`: latest articles, themes, latest editions.
- `/articles/`: every article, by year.
- `/article/<slug>/`: one article, with related articles and previous/next links.
- `/editions/` and `/edition/<slug>/`: the weekly editions, full text.
- `/thematiques/` and `/thematique/<slug>/`: articles by theme.
- `/recherche/`: client-side search over `search.json`.
