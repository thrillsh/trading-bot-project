"""Resume the bot. Removes data/.paused."""
import os

path = 'data/.paused'
if os.path.exists(path):
    os.remove(path)
    print("Bot resumed. It will check normally on its next run.")
else:
    print("Bot was not paused.")
