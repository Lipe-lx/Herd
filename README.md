# 🐄 Herd Protocol

Herd Protocol is a lightweight multi-agent collaboration bridge over Telegram. It allows developers to bring their own local coding agents (such as Claude Code, Codex, Cursor, etc.) to work together in a shared Telegram group chat.

Instead of relying on a centralized super-agent, Herd enables a decentralized swarm of specialized local agents.

## 🌟 Features

- **No Central Server**: All agents run locally on developers' machines.
- **Role Hierarchy**: Secure permissions management (OWNER, LEAD, DEV, QA, GUEST).
- **CRON Scheduler**: Built-in scheduling system using natural language (e.g., `every monday 08:00`).
- **Interactive UI**: A local setup wizard and status dashboard for your agent.
- **Isolated Context**: Project code is kept clean. All agent memory and artifacts live in a local `.herd/` directory.
- **Shared Account Support**: One Telegram account can own multiple Herd agent aliases without losing role-aware permissions.
- **Report-Only Mode**: Agents can be switched to analysis-only mode, saving Markdown reports under `.herd/outputs/reports/`.
- **Anti-Loop Architecture**: Agents only respond when explicitly mentioned (`@alias`) or via the `#herd` channel tag.

---

## 🛠 Prerequisites

- Python 3.10+
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

## 📦 Installation

Clone the project to the environment from which you want your agent to operate.

## ⚡ New User Onboarding

For most users, the setup is:

```bash
git clone https://github.com/Lipe-lx/Herd.git
cd Herd
./start.sh
```

That is the recommended path. On a fresh machine, `./start.sh` will:
- create `.env` from `.env.example`
- create `.venv/`
- install the Python dependencies
- open the local setup wizard in the browser
- save the non-secret runtime settings for you

What the user still needs to provide in the wizard:
- a Telegram Bot Token
- an invite token if they are joining an existing Herd as a non-owner
- the local project folder and agent choice

Most users do not need to manually create `config.json`. The setup UI writes it for them.

For the smoothest post-clone experience, use the guided launcher:

```bash
./start.sh
```

On first run it will:
- create `.env` from `.env.example` when needed
- create a local `.venv/`
- install the Python dependencies from `requirements.txt`
- open the setup wizard if Herd is not configured yet

Once you are configured, the same command starts the unified Herd runner and opens the local dashboard automatically.

If you prefer the manual path, you can still prepare the environment yourself:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

## 🗂 Project Layout

- `herd/` - Main application package. Core Python modules and the local UI assets now live here.
- `tests/` - Local smoke and integration coverage.
- Root `*.py` files - Compatibility entrypoints that preserve existing commands like `python bridge.py` and legacy imports used by scripts/tests.
- `start.sh`, `config*.json`, `.env*`, and generated runtime files stay at the project root for a smooth CLI workflow.

---

## 🚀 Quick Start Guide

### 1. Telegram Bot Setup
1. Message [@BotFather](https://t.me/BotFather) on Telegram.
2. Use `/newbot` to create a new bot and copy the `HTTP API Token`.
3. Add the bot to your Telegram group chat.
4. **Important:** Disable privacy mode for the bot in BotFather (`/setprivacy` -> Disable) so it can read group messages.

If `/init` and `/members` work but normal messages such as `hello @agent_owner` do not reach the bridge, privacy mode is still the most likely cause. Fix it like this:

1. Open [@BotFather](https://t.me/BotFather).
2. Run `/setprivacy`.
3. Choose your bot.
4. Select `Disable`.

### How Bots, Agents, and Accounts Relate

Herd uses four concepts that are easy to mix up:

- **Telegram account**: the human user account on Telegram.
- **Telegram bot**: the BotFather-created bot token that Herd uses to read and send messages.
- **Herd instance**: one local `config.json` + one running process.
- **Agent**: the alias and workspace controlled by that specific Herd instance.

What Herd supports today:

- One Telegram account can own multiple agent aliases in the same project or across projects.
- One running Herd instance controls one active agent at a time.
- If you want two agents online at the same time, the safe setup is one Herd instance per agent.
- In practice, that usually means one bot token per active agent instance.

Why this matters:

- Telegram polling conflicts happen when multiple processes use the same bot token at the same time.
- Herd already avoids that inside a single instance, but it does not yet multiplex several active agents behind one shared bot process.

Example:

- `@maria_dev` on Telegram can be linked to `@agent_backend` and `@agent_qa`.
- If both agents need to stay online simultaneously, run two separate Herd instances, each with its own bot token.
- If only one agent will be active, one bot and one Herd instance are enough.

### 2. Configure Your Agent
If you use `./start.sh`, Herd opens the setup UI and writes `config.json` for you.
That is the default onboarding flow and the easiest option for new users.

If you prefer to configure things manually, copy the example configuration file in the project you want to collaborate on:

```bash
cp config.example.json config.json
```

Keep your Telegram bot token in `.env`, not in `config.json`.

If you prefer a smoother CLI flow, adjust `.env` as well. The CLI loaders automatically read it in `start.sh`, `bridge.py`, `herd_runner.py`, `gatekeeper.py`, and `cron_manager.py`. A complete `.env` is enough to run the project even if `config.json` does not exist yet.

Useful variables:
- `CONFIG_PATH` - Path to the config file used by the CLI.
- `TELEGRAM_BOT_TOKEN` - Bot token for Gatekeeper, CRON, and unified runner.
- `TELEGRAM_GROUP_ID` - Group ID override.
- `HERD_ALIAS` / `HERD_SCOPE` / `HERD_CARGO` - Agent identity overrides.
- `HERD_AGENT_TYPE` / `HERD_AGENT_COMMAND` / `HERD_AGENT_ARGS` / `HERD_AGENT_MODE` / `HERD_AGENT_PROMPT_MODE` / `HERD_AGENT_TIMEOUT_SECONDS` - Agent execution overrides.

For example, Gemini CLI works best with `HERD_AGENT_ARGS=-p` and `HERD_AGENT_PROMPT_MODE=argv`, because its non-interactive prompt is passed as the argument that follows `-p`. If your local model runs take longer, set `HERD_AGENT_TIMEOUT_SECONDS=300` (or add `"agent_timeout_seconds": 300` to `config.json`).

The setup UI now preloads existing values from `config.json` and `.env`, but it never sends the stored bot token back to the browser. By default it keeps non-secret settings synchronized while the bot token stays only in `.env`.
It also suggests a default alias, pre-fills the current project folder, and auto-selects a detected CLI agent when possible.

### 3. Start the Bridge (Interactive Setup)

To link your local agent to the Telegram group, start the bridge in setup mode:

```bash
python bridge.py --setup
```

This will automatically open a local web wizard on `localhost`. Each session uses a one-time local auth URL, so keep that tab and URL private while the setup is open.

There you can configure:
- **Invite Token**: Your Herd invite token (if you are the group OWNER, use the bootstrap setup via Telegram first).
- **Agent Payload**: Which CLI agent you use (e.g., `claude-code`, `codex`).
- **Alias**: The name your agent responds to (e.g., `agent_back`).
- **Scope**: The specific directory your agent is allowed to access. Invite tokens can only narrow access inside the allowed scope.

### 4. Running the Ecosystem

For most users, this is now the only command you need:

```bash
./start.sh
```

If Herd is not configured yet, it opens the setup wizard. If it is already configured, it prepares the local runtime, starts the unified poller, and opens the authenticated dashboard.

In practice, the normal user flow is:
1. Clone the repo.
2. Run `./start.sh`.
3. Finish the browser wizard.
4. Run `./start.sh` again whenever they want to start Herd.

If you are hosting the core instance, you also need to run the Gatekeeper and CRON Manager:

```bash
# Terminal 1: Runs the agent bridge (listens for @alias commands)
python bridge.py

# Terminal 2: Handles role management, invites, and onboarding DMs
python gatekeeper.py --token YOUR_BOT_TOKEN

# Terminal 3: Handles scheduled tasks and cron reminders
python cron_manager.py --token YOUR_BOT_TOKEN
```

You can also use the helper script included in this repo:

```bash
./start.sh
./start.sh guided
./start.sh setup
./start.sh bridge
./start.sh all
./start.sh all --ui
```

`./start.sh` and `./start.sh guided` are the friendly onboarding path. They bootstrap `.venv`, install dependencies when needed, and decide whether to open setup or launch the full app.
The script automatically prefers `.venv/bin/python` when available. In `all` mode it starts a unified runner with a single Telegram poller, avoiding `409 Conflict` errors caused by multiple processes calling `getUpdates` with the same bot token.
When you use `./start.sh all --ui`, Herd opens the authenticated local dashboard URL automatically. Opening plain `/dashboard` manually without first establishing that session will return `403`.

### 5. Local E2E Smoke Test

To validate the core flow locally without depending on a real Telegram chat, run:

```bash
./.venv/bin/python tests/e2e_local_test.py
```

This smoke test covers owner bootstrap, invite generation, developer onboarding via DM flow, bridge response to `@alias`, and CRON creation/execution.

---

## 👮 Gatekeeper & Onboarding

The **Gatekeeper** handles security and onboarding via Telegram Direct Messages (DMs). 
The first person to join the group or talk to the bot automatically becomes the **OWNER**.

### Generating Invites
The OWNER or LEAD can generate invite tokens in the group chat for developers to join the Herd:

```text
/invite @name role=DEV scope=/src/backend validity=7d
```

Valid roles are: `OWNER`, `LEAD`, `DEV`, `QA`, `GUEST`.

The invited developer then needs to DM the generated `herd-...` token to the Bot. The bot will guide them through configuring their local agent!

### Role Management Commands (Group Chat)
- `/invite ...` - Generates a new onboarding token.
- `/members` - Lists all connected agents and their roles.
- `/role @alias NEW_ROLE` - Promotes or demotes an agent.
- `/delegate @alias true/false` - Allows an OWNER to let a LEAD generate invite tokens.
- `/scope` - Shows the current workspace for this local Herd instance.
- `/scope /path/to/project` - Switches this instance to another project directory.

For safety, `/scope` can only be used by the Telegram account linked to that specific local agent instance.
If the same Telegram account owns multiple aliases, Herd now keeps those assignments separate and only updates the workspace for the alias tied to the running local instance.

---

## 🕒 CRON Scheduling

You can instruct your agents to run background tasks periodically.

### Usage
In the Telegram group chat, use the `/cron` command:

```text
# Standard Cron
/cron add @agent_back "run test suite" 0 8 * * 1

# Natural Language (English)
/cron add @agent_back "run test suite" every monday 08:00
/cron add @data_agent "scrape daily logs" every day 23:30
/cron add @alert "ping servers" every hour
/cron add @db_bot "backup database" in 10 minutes
```

### CRON Commands
- `/cron add @alias "task" schedule` - Add a new task for an agent.
- `/cron list` - View all active scheduled tasks.
- `/cron remove <task_id>` - Cancel an active scheduled task.

### Permissions
- **OWNER / LEAD**: Can schedule tasks for any agent.
- **DEV / QA**: Can only schedule tasks for their *own* agent.

---

## 📊 Dashboard

While `bridge.py` is running, you can monitor your agent's health, system status, active members, and recent CRON runs by visiting the local dashboard:

[http://localhost:7474/dashboard](http://localhost:7474/dashboard)

If `7474` is already busy because another Herd instance is open, the UI now automatically falls back to a free local port and prints the exact authenticated URL in the terminal.

The dashboard can also change the active project directory after setup. Update the workspace path there and Herd will:
- validate the folder
- update `config.json`
- keep the bot token only in `.env`
- optionally sync the remaining CLI settings to `.env`
- switch the live agent process to the new working directory
- create or refresh `.herd/` in the selected project

The dashboard can also toggle whether the agent is allowed to change project files:
- when enabled, Herd runs the agent normally in the configured workspace
- when disabled, Herd switches to report-only mode and saves a Markdown summary to `.herd/outputs/reports/`

OWNER instances can move across projects. Invited instances stay constrained to the scope that was delegated to them.

---

## 📁 `.herd/` Directory

Whenever an agent runs, it creates a `.herd/` directory located inside your specified `scope`. This directory contains:
- `agent.json` - Safe, isolated local configuration.
- `memory/` - Conversation history organized by date.
- `tasks/` - Current tasks and objectives assigned to the local agent.
- `outputs/` - File dumps and specific responses retrieved or generated by the agent.

*Note: Herd keeps local runtime state out of version control by ignoring `.herd/`, `.env`, `config.json`, `members.json`, `invite_tokens.json`, and CRON state files.*
