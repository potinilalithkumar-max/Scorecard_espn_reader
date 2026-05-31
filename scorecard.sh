# Install dependencies
pip install discord.py playwright
playwright install chromium

# Set your token as environment variable
export DISCORD_TOKEN="MTUwOTMyNTI4MjY1NjY1MzQ0Mw.GNpPqD.qaJtu4XkP2lQh-CbFifE7Te1jFFy0sT4zssH9k"

# Run the bot
python scorecard.py