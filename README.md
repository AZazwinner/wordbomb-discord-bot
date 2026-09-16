# Word Bomb Discord Bot

Discord bot for the Word Bomb community server. Economy, leaderboards, minigames, and a Flask stats API.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in DISCORD_TOKEN, MONGO_URI, WORDBOMB_API_TOKEN, WORDBOMB_API_TOKEN_PROFILE
python bot.py          # run the bot
python app.py          # run the stats API
```
