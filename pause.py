"""Pause the bot. Creates data/.paused -- bot.py checks for this on every
run and skips all trading activity (including checks) while it exists."""
import os

os.makedirs('data', exist_ok=True)
with open('data/.paused', 'w') as f:
    f.write('Paused via pause.py\n')

print("Bot paused. It will skip all activity until you run: python resume.py")
