# Wardrobe — Closet & Fit Studio

A local, single-user app to track your wardrobe, build looks, and save
collections of outfits. Flask + vanilla HTML/CSS/JS — no build step.

## Run it

1. Install Flask (one time):
   ```
   pip install -r requirements.txt
   ```
2. Start the app:
   ```
   python app.py
   ```
3. Open your browser to: http://localhost:5001

The first run creates `wardrobe.db` and seeds your starting wardrobe and
the **Office** collection. Your data lives in that file (git-ignored).

## What's inside

- **Wardrobe** — add/edit/remove pieces. Every piece can carry a brand,
  color, material, and an ideal temperature range (°F).
- **Fit Maker** — tap pieces to build a look; see them together and get an
  overlapping temperature rating for the whole fit. Save it as an outfit.
- **Collections** — group outfits with their own temperature rating. The
  **Office** collection ships with every combination of your slacks
  (grey, khaki) and your Ralph Lauren long-sleeve polos, rated 65–73°F.
