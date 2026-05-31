# Install dependencies
pip install discord.py playwright
playwright install chromium

# Set your token as environment variable
export DISCORD_TOKEN="my_token"
# Run the bot
python scorecard.py
