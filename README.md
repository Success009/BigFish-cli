# Big Fish CLI

An agentic terminal CLI that drives Google Gemini (AI Studio) or DeepSeek through Chrome, so it needs no API key. Gemini is the default.

## Install or update (Debian / Ubuntu, one command)

```
curl -fsSL https://raw.githubusercontent.com/Success009/BigFish-cli/main/install.sh | sudo bash
```

It installs Chrome, the Python packages, clipboard and audio tools, and the Piper voice. Run the same command again to update an existing install. Then start it with:

```
bf
```

On first start, sign in to your Google account in the Chrome window that opens.

## Commands worth knowing

- `/sudo <password>` saves your sudo password after checking that it works, so commands that need sudo run on their own. A wrong password is not saved. `/sudo` alone asks for it without echo. `/sudo clear` removes the saved password. It is stored in `~/.bigfish_vault.json` (mode 600, base64 encoded, not encrypted).
- `/next [N]` and `/transfer [N]` take the account number (the number in the account email, for example 13), not a list position. Without a number they pick the next usable profile and stop if none is available.
- `/tokens` shows usage per profile.

## Windows and focus

Chrome is not brought to the front when a prompt is sent. Set `BF_RAISE_BROWSER=1` to turn raising back on. After a sub-agent Chrome opens, focus returns to the window you were in (needs `xdotool`).

## Logs

A reply read from the page instead of the network gets a red notice under the timing line. Details are in `~/gemini_local_agent/ui_fallback.log`, and `capture_history.log` keeps the last 500 turns.
