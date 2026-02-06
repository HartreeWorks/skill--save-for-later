---
name: save-for-later
description: This skill should be used when the user says "/later", "save for later", "save this session", "come back to this later", "park this conversation", "/later list", "later list", "what did I save", "saved conversations", "resume saved", or mentions wanting to bookmark a conversation for later resumption.
---

# Save for later

Save Claude Code conversations to a registry for later resumption, eliminating the need to keep terminal sessions open as reminders of unfinished work.

## How it works

Claude Code stores session IDs in `~/.claude/history.jsonl`. This skill saves the current session ID and a description to a JSON registry. Later, conversations can be listed and resumed using `claude --resume <session-id>`.

Registry location: `~/.claude/skills/save-for-later/registry.json`
Script location: `~/.claude/skills/save-for-later/scripts/later.py`

## Finding the current session ID

To identify the current session, look up the most recent entry in `~/.claude/history.jsonl` matching the current working directory:

```bash
grep "$(pwd)" ~/.claude/history.jsonl | tail -1 | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['sessionId'])"
```

## Saving a conversation

When the user says "/later" or "save for later":

1. Find the current session ID using the method above
2. Ask the user for a brief description of the task (or use the first prompt as fallback)
3. Run the save command:

```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py save \
  --session-id SESSION_ID \
  --project "$(pwd)" \
  --description "Brief description of the task"
```

4. Confirm the save and remind the user they can now safely close this session
5. **Then offer to save/close other sessions** — run the discover flow below

## Discovering and closing other sessions

After saving the current session (or when the user wants to tidy up), discover other active Claude sessions and offer to save or close them.

### Step 1: Discover active sessions

```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py discover --exclude-pid CURRENT_PID
```

To find the current PID: `echo $$` in bash, or use `os.getpid()`.

This outputs JSON with all active interactive claude sessions, including context extracted from their session files:
- `firstPrompt`: what the conversation started with
- `lastAssistantResponse`: what Claude last said (truncated)
- `lastUserMessage`: what the user last said
- `recentTools`: last 5 tools used (e.g. Edit, Bash, Grep)

### Step 2: Present the list to the user

Format the discovered sessions as a numbered list showing:
- **Project name** and when it started
- **First prompt** (what the task was about)
- **Last activity** (what happened most recently — the last assistant response)
- **CPU usage** (helps identify idle vs active sessions)

Example format:
```
1. [my-web-app] Started Wed — 3.2% CPU
   Task: "refactor the authentication module, then help me..."
   Last: "Done. Extracted shared logic into a helper function..."

2. [api-backend] Started Mon — 16% CPU
   Task: "debug the caching issue in the feed endpoint..."
   Last: "Fixed the cache invalidation logic and added a test..."
```

### Step 3: Ask user what to do with each

Present options for each session. The user can respond with numbers:
- **Save for later**: save to registry, then kill the process
- **Close** (done/not needed): just kill the process without saving
- **Keep running**: leave it alone

### Step 4: Execute

For each session the user wants to save:
```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py save \
  --session-id SESSION_ID \
  --project CWD \
  --description "Description based on context"
```

For each session to close (saved or not):
```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py kill --pid PID
```

## Listing saved conversations

When the user says "/later list", "what did I save", or "saved conversations":

```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py list
```

To include completed/removed entries:

```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py list --all
```

After showing the list, offer to resume any active conversation. To resume:

```
claude --resume <session-id>
```

## Marking a conversation as done

```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py done --id ID_NUMBER
```

## Removing a conversation

```bash
python3 ~/.claude/skills/save-for-later/scripts/later.py remove --id ID_NUMBER
```

## Deciding between save and list

- `/later` — **save** the current session, then offer to save/close others
- `/later list` — **list** saved sessions
- "save for later", "park this", "come back to this later" — **save**
- "what did I save", "saved conversations", "what's parked", "resume saved" — **list**
- If ambiguous, ask the user whether they want to save the current session or review saved ones

## Update check

This is a shared skill. Before executing, check `~/.claude/skills/.update-config.json`.
If `auto_check_enabled` is true and `last_checked_timestamp` is older than `check_frequency_days`,
mention: "It's been a while since skill updates were checked. Run `/update-skills` to see available updates."
Do NOT perform network operations - just check the local timestamp.
