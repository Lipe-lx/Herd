import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bridge
import cron_manager
import gatekeeper
import setup_diagnostics
from env_loader import env_values_from_config, load_env, write_env_file

ENV_KEYS = [
    "ENV_PATH",
    "CONFIG_PATH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_GROUP_ID",
    "TELEGRAM_USER_ID",
    "HERD_INVITE_TOKEN",
    "HERD_ALIAS",
    "HERD_SCOPE",
    "HERD_CARGO",
    "HERD_ALLOW_PROJECT_CHANGES",
    "HERD_AGENT_TYPE",
    "HERD_AGENT_COMMAND",
    "HERD_AGENT_ARGS",
    "HERD_AGENT_MODE",
    "HERD_AGENT_PROMPT_MODE",
    "HERD_AGENT_TIMEOUT_SECONDS",
]


class FakeUser:
    def __init__(self, user_id: int, username: str, is_bot: bool = False, first_name: str | None = None):
        self.id = user_id
        self.username = username
        self.is_bot = is_bot
        self.first_name = first_name or username


class FakeChat:
    def __init__(self, chat_id: int, chat_type: str):
        self.id = chat_id
        self.type = chat_type


class FakeMessage:
    _next_message_id = 100

    def __init__(self, text, chat: FakeChat, from_user: FakeUser, new_chat_members=None, message_thread_id=None):
        self.text = text
        self.chat = chat
        self.from_user = from_user
        self.new_chat_members = new_chat_members or []
        self.replies = []
        self.message_thread_id = message_thread_id
        self.message_id = FakeMessage._next_message_id
        FakeMessage._next_message_id += 1

    async def reply_text(self, text: str, **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})


class FakeUpdate:
    def __init__(self, message: FakeMessage):
        self.message = message


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.chat_actions = []
        self.deleted_messages = []
        self._next_message_id = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs):
        message_id = self._next_message_id
        self._next_message_id += 1
        payload = {"chat_id": chat_id, "text": text, "kwargs": kwargs, "message_id": message_id}
        self.sent_messages.append(payload)
        return mock.Mock(message_id=message_id)

    async def send_chat_action(self, chat_id: int, action: str, **kwargs):
        self.chat_actions.append({"chat_id": chat_id, "action": action, "kwargs": kwargs})

    async def delete_message(self, chat_id: int, message_id: int, **kwargs):
        self.deleted_messages.append({"chat_id": chat_id, "message_id": message_id, "kwargs": kwargs})


class FakeContext:
    def __init__(self, bot: FakeBot, bot_data=None):
        self.bot = bot
        self.bot_data = bot_data or {}


class HerdLocalE2ETest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)

        self.scope = Path(self.tmpdir.name) / "project"
        self.scope.mkdir(parents=True, exist_ok=True)

        self.config = {
            "telegram_bot_token": "test-token",
            "telegram_group_id": -100123,
            "telegram_id": 0,
            "invite_token": None,
            "alias": "agent_dev",
            "agent": {
                "type": "fake",
                "command": "fake-agent",
                "args": [],
                "mode": "cli",
            },
            "scope": str(self.scope),
            "allowed_scope": str(self.scope),
            "cargo": "DEV",
            "allow_project_changes": True,
            "auto_register": True,
            "proactive_events": {
                "on_commit": False,
                "on_build_fail": False,
                "on_pr_open": False,
            },
        }
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        bridge.bootstrap_herd_dir(self.config["scope"], self.config)

        gatekeeper.ONBOARDING_SESSIONS.clear()
        self.bot = FakeBot()
        self.group_chat = FakeChat(self.config["telegram_group_id"], "group")
        self.dm_chat = FakeChat(200, "private")
        self.owner = FakeUser(100, "owner_user")
        self.dev = FakeUser(200, "dev_user")

        self.original_call_agent = bridge.call_agent
        bridge.call_agent = lambda config, message_text, herd_path, context_lines=20: "Resposta simulada do agent"
        self.env_backup = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        bridge.call_agent = self.original_call_agent
        gatekeeper.ONBOARDING_SESSIONS.clear()
        for key in ENV_KEYS:
            if self.env_backup[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self.env_backup[key]
        os.chdir(self.prev_cwd)
        self.tmpdir.cleanup()

    async def test_full_local_flow(self):
        await self._bootstrap_owner()
        token = await self._invite_developer()
        await self._complete_onboarding(token)
        await self._exercise_bridge()
        await self._exercise_cron()

        members = gatekeeper.load_members()["members"]
        self.assertEqual(len(members), 2)
        self.assertEqual({member["alias"] for member in members}, {"owner", "agent_dev"})

        history = cron_manager.load_history()["runs"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["alias"], "agent_dev")

        sent_texts = [entry["text"] for entry in self.bot.sent_messages]
        self.assertTrue(any("Welcome to Herd" in text for text in sent_texts))
        self.assertTrue(any("Resposta simulada do agent" in text for text in sent_texts))
        self.assertTrue(any("[CRON -> @agent_dev]" in text for text in sent_texts))

    def test_env_only_config_and_config_file_precedence(self):
        env_path = Path(self.tmpdir.name) / ".env"
        config_with_timeout = dict(self.config)
        config_with_timeout["agent_timeout_seconds"] = 240
        write_env_file(env_values_from_config(config_with_timeout, config_path=Path("missing-config.json")), env_path=env_path)
        os.environ["ENV_PATH"] = str(env_path)
        load_env()

        env_only_config = bridge.load_config(Path("missing-config.json"))
        self.assertTrue(bridge.is_config_complete(env_only_config))
        self.assertEqual(env_only_config["alias"], "agent_dev")
        self.assertEqual(env_only_config["telegram_group_id"], -100123)
        self.assertEqual(env_only_config["agent_timeout_seconds"], 240)

        file_config = dict(self.config)
        file_config["alias"] = "from_file"
        Path("file-config.json").write_text(json.dumps(file_config, indent=2), encoding="utf-8")

        merged = bridge.load_config(Path("file-config.json"))
        self.assertEqual(merged["alias"], "from_file")

        saved = bridge.save_runtime_config(config_with_timeout, Path("saved-config.json"), sync_env=True)
        self.assertTrue(Path(saved["config_path"]).exists())
        self.assertTrue(Path(saved["env_path"]).exists())

        saved_config = json.loads(Path(saved["config_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("telegram_bot_token", saved_config)

        saved_env = Path(saved["env_path"]).read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_BOT_TOKEN=test-token", saved_env)
        self.assertIn("HERD_ALLOW_PROJECT_CHANGES=true", saved_env)
        self.assertIn("HERD_AGENT_TIMEOUT_SECONDS=240", saved_env)

    def test_save_runtime_config_updates_gitignore_for_sensitive_instance_files(self):
        os.environ["ENV_PATH"] = str(Path("runtime") / "second-agent.env")

        saved = bridge.save_runtime_config(
            self.config,
            Path("instances") / "second-agent.runtime.json",
            sync_env=True,
        )

        self.assertTrue(Path(saved["config_path"]).exists())
        self.assertTrue(Path(saved["env_path"]).exists())

        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env*", gitignore)
        self.assertIn("!.env.example", gitignore)
        self.assertIn("config*.json", gitignore)
        self.assertIn("!config.example.json", gitignore)
        self.assertIn("members.json", gitignore)
        self.assertIn("invite_tokens.json", gitignore)
        self.assertIn("instances/second-agent.runtime.json", gitignore)
        self.assertIn("runtime/second-agent.env", gitignore)

    def test_cli_agent_uses_stdin_by_default(self):
        completed = mock.Mock(returncode=0, stdout="ok via stdin", stderr="")
        agent_cfg = {
            "type": "claude-code",
            "command": "claude",
            "args": ["--print"],
            "mode": "cli",
        }

        with mock.patch("bridge.subprocess.run", return_value=completed) as run_mock:
            response = bridge._call_agent_cli(agent_cfg, "responda ok", str(self.scope))

        self.assertEqual(response, "ok via stdin")
        self.assertEqual(run_mock.call_args.args[0], ["claude", "--print"])
        self.assertEqual(run_mock.call_args.kwargs["input"], "responda ok")
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 120)

    def test_cli_agent_uses_argv_when_prompt_flag_is_present(self):
        completed = mock.Mock(returncode=0, stdout="ok via argv", stderr="")
        agent_cfg = {
            "type": "gemini",
            "command": "gemini",
            "args": ["-p"],
            "mode": "cli",
            "prompt_mode": "argv",
        }

        with mock.patch("bridge.subprocess.run", return_value=completed) as run_mock:
            response = bridge._call_agent_cli(agent_cfg, "responda ok", str(self.scope))

        self.assertEqual(response, "ok via argv")
        self.assertEqual(run_mock.call_args.args[0], ["gemini", "-p", "responda ok"])
        self.assertIsNone(run_mock.call_args.kwargs["input"])
        self.assertEqual(run_mock.call_args.kwargs["stdin"], bridge.subprocess.DEVNULL)
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 300)

    def test_execute_agent_prompt_applies_top_level_timeout_override(self):
        completed = mock.Mock(returncode=0, stdout="ok override", stderr="")
        config = dict(self.config)
        config["agent_timeout_seconds"] = 45

        with mock.patch("bridge.subprocess.run", return_value=completed) as run_mock:
            response = bridge.execute_agent_prompt(
                config,
                "responda ok",
                Path(self.config["scope"]) / ".herd",
            )

        self.assertEqual(response, "ok override")
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 45)

    def test_build_agent_prompt_enforces_same_language_and_final_answer_only(self):
        prompt = bridge.build_agent_prompt(
            "@agent_owner responda apenas: ok",
            "Recent context from the group channel:\n[@dev]: oi",
        )

        self.assertIn("same language as the user's latest message", prompt)
        self.assertIn("Output only the final user-facing answer", prompt)
        self.assertIn("Recent context from the group channel", prompt)
        self.assertIn("@agent_owner responda apenas: ok", prompt)

    def test_finalize_agent_response_removes_leading_reasoning(self):
        raw = (
            "I will respond as requested by the user.\n"
            "I will now inspect the config and members.\n\n"
            "Minhas atribuições são:\n"
            "1. Gerenciar membros.\n"
            "2. Manter a instância."
        )

        cleaned = bridge.finalize_agent_response(raw)

        self.assertTrue(cleaned.startswith("Minhas atribuições são:"))
        self.assertNotIn("I will respond", cleaned)
        self.assertNotIn("I will now inspect", cleaned)

    async def test_post_to_group_uses_telegram_html_formatting(self):
        await bridge.post_to_group(
            self.bot,
            self.config["telegram_group_id"],
            "agent_dev",
            "**Resumo**\n- item\n`cmd`",
        )

        sent = self.bot.sent_messages[-1]
        self.assertEqual(sent["kwargs"]["parse_mode"], "HTML")
        self.assertTrue(sent["kwargs"]["disable_web_page_preview"])
        self.assertIn("<b>@agent_dev</b>", sent["text"])
        self.assertIn("<b>Resumo</b>", sent["text"])
        self.assertIn("• item", sent["text"])
        self.assertIn("<code>cmd</code>", sent["text"])

    async def test_post_to_group_keeps_message_thread(self):
        await bridge.post_to_group(
            self.bot,
            self.config["telegram_group_id"],
            "agent_dev",
            "ok",
            message_thread_id=42,
        )

        sent = self.bot.sent_messages[-1]
        self.assertEqual(sent["kwargs"]["message_thread_id"], 42)

    async def test_call_agent_with_feedback_sends_typing_action(self):
        original_call_agent = bridge.call_agent

        def slow_call_agent(config, message_text, herd_path, context_lines=20):
            import time

            time.sleep(0.05)
            return "ok"

        bridge.call_agent = slow_call_agent
        try:
            response = await bridge.call_agent_with_feedback(
                self.bot,
                self.config["telegram_group_id"],
                self.config,
                "@agent_dev responda",
                Path(self.config["scope"]) / ".herd",
            )
        finally:
            bridge.call_agent = original_call_agent

        self.assertEqual(response, "ok")
        self.assertTrue(self.bot.chat_actions)
        self.assertEqual(self.bot.chat_actions[0]["chat_id"], self.config["telegram_group_id"])
        self.assertEqual(self.bot.chat_actions[0]["action"], "typing")

    async def test_call_agent_with_feedback_sends_temporary_processing_notice(self):
        original_call_agent = bridge.call_agent
        original_delay = bridge.PROCESSING_NOTICE_DELAY_SECONDS

        def slow_call_agent(config, message_text, herd_path, context_lines=20):
            import time

            time.sleep(0.08)
            return "ok"

        bridge.call_agent = slow_call_agent
        bridge.PROCESSING_NOTICE_DELAY_SECONDS = 0.01
        try:
            response = await bridge.call_agent_with_feedback(
                self.bot,
                self.config["telegram_group_id"],
                self.config,
                "@agent_dev responda",
                Path(self.config["scope"]) / ".herd",
                message_thread_id=77,
                reply_to_message_id=501,
            )
        finally:
            bridge.call_agent = original_call_agent
            bridge.PROCESSING_NOTICE_DELAY_SECONDS = original_delay

        self.assertEqual(response, "ok")
        self.assertEqual(self.bot.sent_messages[0]["text"], "⏳")
        self.assertEqual(self.bot.sent_messages[0]["kwargs"]["message_thread_id"], 77)
        self.assertEqual(self.bot.sent_messages[0]["kwargs"]["reply_to_message_id"], 501)
        self.assertTrue(self.bot.deleted_messages)
        self.assertEqual(self.bot.deleted_messages[0]["message_id"], self.bot.sent_messages[0]["message_id"])

    def test_normalize_alias_accepts_human_friendly_input(self):
        self.assertEqual(setup_diagnostics.normalize_alias("@Agent Back!!"), "agent_back")

    def test_setup_diagnostics_prefill_scope_and_detect_agent(self):
        with mock.patch("setup_diagnostics.shutil.which", side_effect=lambda cmd: "/usr/bin/codex" if cmd == "codex" else None):
            diagnostics = setup_diagnostics.build_setup_diagnostics(
                {},
                project_root=self.scope,
                config_path=Path("config.json"),
                env_path=Path(".env"),
            )

        self.assertEqual(diagnostics["defaults"]["scope"], str(self.scope))
        self.assertEqual(diagnostics["defaults"]["alias"], "agent_project")
        self.assertEqual(diagnostics["recommended_agent"], "codex")
        self.assertIn("Codex", diagnostics["detected_agent_labels"])

    def test_custom_agent_command_is_split_for_setup(self):
        handler = object.__new__(bridge.HerdUIHandler)

        agent_cfg = handler._build_agent_config({
            "agent": "other",
            "command": 'my-agent --run "hello world"',
        })

        self.assertEqual(agent_cfg["command"], "my-agent")
        self.assertEqual(agent_cfg["args"], ["--run", "hello world"])

    def test_setup_reconfigure_flow_does_not_require_invite_token(self):
        self.config["cargo"] = "OWNER"
        self.config["alias"] = "agent_owner"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")

        handler = object.__new__(bridge.HerdUIHandler)
        handler.server = mock.Mock(bridge_state={"config_path": Path("config.json"), "status": {}})

        result = handler._run_setup({
            "flow": "reconfigure",
            "bootstrap": False,
            "use_stored_token": True,
            "sync_env": False,
            "telegram_group_id": self.config["telegram_group_id"],
            "agent": "codex",
            "alias": "agent_reconfigured",
            "scope": str(self.scope),
            "allow_project_changes": True,
        })

        self.assertTrue(result["success"])
        saved_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_config["alias"], "agent_reconfigured")
        self.assertEqual(saved_config["cargo"], "OWNER")

    def test_setup_uses_last_pairing_identity_when_body_does_not_include_telegram_id(self):
        handler = object.__new__(bridge.HerdUIHandler)
        handler.server = mock.Mock(bridge_state={
            "config_path": Path("config.second-agent.json"),
            "status": {},
            "last_pairing": {
                "token": "test-bot-token",
                "group_id": self.config["telegram_group_id"],
                "telegram_id": self.dev.id,
                "telegram_username": self.dev.username,
            },
        })

        with mock.patch.object(
            bridge.HerdUIHandler,
            "_validate_token",
            return_value={"valid": True, "role": "DEV", "scope": str(self.scope)},
        ):
            result = handler._run_setup({
                "flow": "join",
                "bootstrap": False,
                "token": "herd-test-token",
                "telegram_bot_token": "test-bot-token",
                "telegram_group_id": self.config["telegram_group_id"],
                "agent": "codex",
                "alias": "agent_dev",
                "scope": str(self.scope),
                "allow_project_changes": True,
                "sync_env": False,
            })

        self.assertTrue(result["success"])
        saved_config = json.loads(Path("config.second-agent.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_config["telegram_id"], self.dev.id)
        members = gatekeeper.load_members()["members"]
        self.assertTrue(any(member["alias"] == "agent_dev" and member["telegram_id"] == self.dev.id for member in members))

    def test_upsert_member_registration_updates_existing_alias(self):
        bridge.upsert_member_registration({
            "telegram_id": self.owner.id,
            "telegram_username": self.owner.username,
            "alias": "agent_owner",
            "agent": {"type": "gemini"},
            "scope": self.config["scope"],
            "cargo": "OWNER",
            "invite_token": "bootstrap",
        })
        bridge.upsert_member_registration({
            "telegram_id": self.owner.id,
            "telegram_username": self.owner.username,
            "alias": "agent_owner",
            "agent": {"type": "codex"},
            "scope": "/new-scope",
            "cargo": "OWNER",
            "invite_token": "bootstrap",
        })

        members = gatekeeper.load_members()["members"]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["agent"], "codex")
        self.assertEqual(members[0]["scope"], "/new-scope")

    def test_ui_landing_path_prefers_dashboard_when_configured(self):
        bridge_state = {
            "status": {"setup_complete": True},
            "config_path": Path("config.json"),
            "config_ref": dict(self.config),
        }

        self.assertEqual(bridge.resolve_ui_landing_path(bridge_state), "/dashboard")

    def test_ui_landing_path_prefers_setup_when_not_configured(self):
        incomplete_config = dict(self.config)
        incomplete_config.pop("telegram_group_id", None)
        incomplete_path = Path("incomplete-config.json")
        incomplete_path.write_text(json.dumps(incomplete_config, indent=2), encoding="utf-8")
        bridge_state = {
            "status": {"setup_complete": False},
            "config_path": incomplete_path,
            "config_ref": incomplete_config,
        }

        self.assertEqual(bridge.resolve_ui_landing_path(bridge_state), "/setup")

    def test_start_ui_server_falls_back_to_free_port_when_default_is_busy(self):
        busy_error = OSError(98, "Address already in use")
        fallback_server = mock.Mock()
        fallback_server.server_address = ("127.0.0.1", 38123)

        with mock.patch("bridge.ThreadingHTTPServer", side_effect=[busy_error, fallback_server]) as server_mock, \
             mock.patch("bridge.threading.Thread") as thread_mock, \
             mock.patch("bridge.secrets.token_urlsafe", return_value="test-auth-token"):
            thread_instance = mock.Mock()
            thread_mock.return_value = thread_instance

            bridge_state = {"status": {}, "config_path": Path("config.json"), "config_ref": dict(self.config)}
            server = bridge.start_ui_server(bridge_state, open_browser=False, initial_path="/setup")

        self.assertIs(server, fallback_server)
        self.assertEqual(bridge_state["status"]["ui_port"], 38123)
        self.assertEqual(server_mock.call_args_list[0].args[0], ("localhost", bridge.UI_PORT))
        self.assertEqual(server_mock.call_args_list[1].args[0], ("localhost", 0))
        thread_mock.assert_called_once()
        thread_instance.start.assert_called_once()

    def test_upsert_member_registration_adds_second_alias_for_same_account(self):
        bridge.upsert_member_registration({
            "telegram_id": self.owner.id,
            "telegram_username": self.owner.username,
            "alias": "agent_owner",
            "agent": {"type": "gemini"},
            "scope": self.config["scope"],
            "cargo": "OWNER",
            "invite_token": "bootstrap",
        })
        bridge.upsert_member_registration({
            "telegram_id": self.owner.id,
            "telegram_username": self.owner.username,
            "alias": "agent_dev",
            "agent": {"type": "codex"},
            "scope": self.config["scope"],
            "cargo": "DEV",
            "invite_token": "herd-join",
        })

        members = gatekeeper.load_members()["members"]
        self.assertEqual(len(members), 2)
        self.assertEqual({member["alias"] for member in members}, {"agent_owner", "agent_dev"})
        self.assertEqual({member["telegram_id"] for member in members}, {self.owner.id})

    def test_update_runtime_scope_keeps_live_config_and_env_in_sync(self):
        new_scope = Path(self.tmpdir.name) / "other-project"
        new_scope.mkdir(parents=True, exist_ok=True)

        env_path = Path(self.tmpdir.name) / ".env"
        os.environ["ENV_PATH"] = str(env_path)

        live_config = dict(self.config)
        live_config["cargo"] = "OWNER"
        Path("config.json").write_text(json.dumps(live_config, indent=2), encoding="utf-8")
        live_bot_data = {
            "config": live_config,
            "herd_path": Path(self.config["scope"]) / ".herd",
        }
        bridge_state = {
            "config_path": Path("config.json"),
            "config_ref": live_config,
            "bot_data_ref": live_bot_data,
            "status": {"online": True, "setup_complete": True, "alias": self.config["alias"], "scope": self.config["scope"]},
            "herd_path": str(Path(self.config["scope"]) / ".herd"),
        }

        updated = bridge.update_runtime_scope(str(new_scope), bridge_state, sync_env=True)

        saved_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_config["scope"], str(new_scope))
        self.assertEqual(saved_config["allowed_scope"], str(new_scope))
        self.assertEqual(live_config["scope"], str(new_scope))
        self.assertEqual(live_bot_data["config"]["scope"], str(new_scope))
        self.assertEqual(live_bot_data["herd_path"], new_scope / ".herd")
        self.assertEqual(bridge_state["herd_path"], str(new_scope / ".herd"))
        self.assertEqual(bridge_state["status"]["scope"], str(new_scope))
        self.assertEqual(updated["scope"], str(new_scope))

        env_contents = env_path.read_text(encoding="utf-8")
        self.assertIn(f"HERD_SCOPE={new_scope}", env_contents)
        self.assertTrue((new_scope / ".herd" / "agent.json").exists())

        agent_metadata = json.loads((new_scope / ".herd" / "agent.json").read_text(encoding="utf-8"))
        self.assertEqual(agent_metadata["scope"], str(new_scope))
        self.assertEqual(agent_metadata["alias"], self.config["alias"])
        self.assertEqual(agent_metadata["allowed_scope"], str(new_scope))
        self.assertTrue(agent_metadata["allow_project_changes"])

    def test_update_runtime_scope_rejects_non_owner_outside_allowed_scope(self):
        new_scope = Path(self.tmpdir.name) / "outside-scope"
        new_scope.mkdir(parents=True, exist_ok=True)

        bridge_state = {
            "config_path": Path("config.json"),
            "config_ref": dict(self.config),
            "status": {"online": True, "setup_complete": True, "alias": self.config["alias"], "scope": self.config["scope"]},
            "herd_path": str(Path(self.config["scope"]) / ".herd"),
        }

        with self.assertRaisesRegex(ValueError, "allowed scope"):
            bridge.update_runtime_scope(str(new_scope), bridge_state, sync_env=True)

    async def test_scope_command_updates_workspace_live_for_bound_account(self):
        self.config["cargo"] = "OWNER"
        self.config["alias"] = "agent_owner"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [{
                "telegram_id": self.owner.id,
                "telegram_username": self.owner.username,
                "alias": "agent_owner",
                "agent": "gemini",
                "scope": self.config["scope"],
                "cargo": "OWNER",
                "token_delegation": True,
                "invited_by": "system",
                "invite_token": "bootstrap",
                "registered_at": "now",
                "online": True,
            }]
        })

        env_path = Path(self.tmpdir.name) / ".env"
        os.environ["ENV_PATH"] = str(env_path)

        live_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        bot_data = {
            "config": live_config,
            "herd_path": Path(self.config["scope"]) / ".herd",
        }
        bridge_state = {
            "config_path": Path("config.json"),
            "config_ref": live_config,
            "bot_data_ref": bot_data,
            "status": {"online": True, "setup_complete": True, "alias": "agent_owner", "scope": self.config["scope"]},
            "herd_path": str(Path(self.config["scope"]) / ".herd"),
        }
        bot_data["bridge_state"] = bridge_state

        new_scope = Path(self.tmpdir.name) / "telegram-switch"
        new_scope.mkdir(parents=True, exist_ok=True)
        message = FakeMessage(f"/scope {new_scope}", self.group_chat, self.owner)
        context = FakeContext(self.bot, bot_data=bot_data)

        await gatekeeper.dispatch_command(FakeUpdate(message), context)

        self.assertTrue(message.replies)
        self.assertIn("Workspace updated", message.replies[-1]["text"])
        self.assertIn(str(new_scope), message.replies[-1]["text"])

        saved_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_config["scope"], str(new_scope))
        self.assertEqual(bot_data["config"]["scope"], str(new_scope))
        self.assertEqual(bot_data["herd_path"], new_scope / ".herd")

        members = gatekeeper.load_members()["members"]
        self.assertEqual(members[0]["scope"], str(new_scope))
        self.assertTrue((new_scope / ".herd" / "agent.json").exists())

    async def test_scope_command_rejects_other_group_members(self):
        self.config["cargo"] = "OWNER"
        self.config["alias"] = "agent_owner"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_owner",
                    "agent": "gemini",
                    "scope": self.config["scope"],
                    "cargo": "OWNER",
                    "token_delegation": True,
                    "invited_by": "system",
                    "invite_token": "bootstrap",
                    "registered_at": "now",
                    "online": True,
                },
                {
                    "telegram_id": self.dev.id,
                    "telegram_username": self.dev.username,
                    "alias": "agent_dev",
                    "agent": "codex",
                    "scope": "/somewhere-else",
                    "cargo": "DEV",
                    "token_delegation": False,
                    "invited_by": "agent_owner",
                    "invite_token": "herd-test",
                    "registered_at": "now",
                    "online": True,
                },
            ]
        })

        attempted_scope = Path(self.tmpdir.name) / "unauthorized-switch"
        attempted_scope.mkdir(parents=True, exist_ok=True)
        message = FakeMessage(f"/scope {attempted_scope}", self.group_chat, self.dev)

        await gatekeeper.dispatch_command(FakeUpdate(message), FakeContext(self.bot, bot_data={}))

        self.assertTrue(message.replies)
        self.assertIn("Only the Telegram account linked to this Herd instance", message.replies[-1]["text"])

        saved_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_config["scope"], self.config["scope"])

    async def test_admin_commands_are_ignored_by_lower_role_instance_on_shared_account(self):
        self.config["cargo"] = "DEV"
        self.config["alias"] = "agent_dev"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_owner",
                    "agent": "gemini",
                    "scope": self.config["scope"],
                    "cargo": "OWNER",
                    "token_delegation": True,
                    "invited_by": "system",
                    "invite_token": "bootstrap",
                    "registered_at": "now",
                    "online": True,
                },
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_dev",
                    "agent": "codex",
                    "scope": self.config["scope"],
                    "cargo": "DEV",
                    "token_delegation": False,
                    "invited_by": "agent_owner",
                    "invite_token": "herd-test",
                    "registered_at": "now",
                    "online": True,
                },
            ]
        })

        members_message = FakeMessage("/members", self.group_chat, self.owner)
        invite_message = FakeMessage(f"/invite @agent_new role=DEV scope={self.scope}", self.group_chat, self.owner)

        await gatekeeper.dispatch_command(FakeUpdate(members_message), FakeContext(self.bot))
        await gatekeeper.dispatch_command(FakeUpdate(invite_message), FakeContext(self.bot))

        self.assertFalse(members_message.replies)
        self.assertFalse(invite_message.replies)
        self.assertEqual(gatekeeper.load_tokens()["tokens"], [])

    async def test_agents_command_lists_copyable_aliases_for_registered_members(self):
        gatekeeper.save_members({
            "members": [
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_owner",
                    "agent": "gemini",
                    "scope": self.config["scope"],
                    "cargo": "OWNER",
                    "token_delegation": True,
                    "invited_by": "system",
                    "invite_token": "bootstrap",
                    "registered_at": "now",
                    "online": True,
                },
                {
                    "telegram_id": self.dev.id,
                    "telegram_username": self.dev.username,
                    "alias": "agent_dev",
                    "agent": "codex",
                    "scope": self.config["scope"],
                    "cargo": "DEV",
                    "token_delegation": False,
                    "invited_by": "agent_owner",
                    "invite_token": "herd-test",
                    "registered_at": "now",
                    "online": True,
                },
            ]
        })

        message = FakeMessage("/agents", self.group_chat, self.dev)

        await gatekeeper.dispatch_command(FakeUpdate(message), FakeContext(self.bot))

        self.assertTrue(message.replies)
        reply = message.replies[-1]
        self.assertIn("Agent aliases", reply["text"])
        self.assertIn("`@agent_dev`", reply["text"])
        self.assertIn("`@agent_owner`", reply["text"])
        self.assertEqual(reply["kwargs"].get("parse_mode"), "Markdown")

    async def test_group_init_bootstraps_owner_from_runtime_config(self):
        self.config["cargo"] = "OWNER"
        self.config["alias"] = "agent_owner"
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")

        init_message = FakeMessage(
            text="/init 8703",
            chat=self.group_chat,
            from_user=self.owner,
        )
        await gatekeeper.dispatch_command(FakeUpdate(init_message), FakeContext(self.bot))

        members = gatekeeper.load_members()["members"]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["cargo"], "OWNER")
        self.assertEqual(members[0]["telegram_id"], self.owner.id)
        self.assertEqual(members[0]["alias"], "agent_owner")

        updated_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(updated_config["telegram_id"], self.owner.id)
        self.assertEqual(updated_config["telegram_group_id"], self.group_chat.id)

        self.assertTrue(init_message.replies)
        self.assertIn("Herd initialized", init_message.replies[-1]["text"])

    async def test_group_init_is_silently_ignored_by_non_owner_instance(self):
        self.config["cargo"] = "DEV"
        self.config["alias"] = "agent_dev"
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")

        init_message = FakeMessage(
            text="/init 8703",
            chat=self.group_chat,
            from_user=self.dev,
        )
        await gatekeeper.dispatch_command(FakeUpdate(init_message), FakeContext(self.bot))

        self.assertFalse(init_message.replies)

    def test_public_config_state_redacts_bot_token(self):
        state = bridge.get_current_config_state(Path("config.json"))

        self.assertTrue(state["has_bot_token"])
        self.assertNotIn("telegram_bot_token", state["config"])
        self.assertEqual(state["config"]["scope"], str(self.scope))
        self.assertIn("diagnostics", state)
        self.assertEqual(state["diagnostics"]["defaults"]["scope"], self.tmpdir.name)

    async def test_bridge_ignores_private_messages(self):
        gatekeeper.save_members({
            "members": [{
                "telegram_id": self.dev.id,
                "telegram_username": self.dev.username,
                "alias": "agent_dev",
                "agent": "codex",
                "scope": self.config["scope"],
                "cargo": "DEV",
                "token_delegation": False,
                "invited_by": "owner",
                "invite_token": "herd-test",
                "registered_at": "now",
                "online": True,
            }]
        })
        context = FakeContext(
            self.bot,
            bot_data={
                "config": self.config,
                "herd_path": Path(self.config["scope"]) / ".herd",
            },
        )
        message = FakeMessage(
            text="@agent_dev responde em DM",
            chat=self.dm_chat,
            from_user=self.dev,
        )

        await bridge.handle_message(FakeUpdate(message), context)

        self.assertFalse(self.bot.sent_messages)

    async def test_bridge_ignores_unregistered_group_sender(self):
        context = FakeContext(
            self.bot,
            bot_data={
                "config": self.config,
                "herd_path": Path(self.config["scope"]) / ".herd",
            },
        )
        outsider = FakeUser(999, "outsider")
        message = FakeMessage(
            text="@agent_dev tenta executar algo",
            chat=self.group_chat,
            from_user=outsider,
        )

        await bridge.handle_message(FakeUpdate(message), context)

        self.assertFalse(self.bot.sent_messages)

    async def test_bridge_sanitizes_reasoning_before_sending_to_group(self):
        bridge.call_agent = lambda config, message_text, herd_path, context_lines=20: (
            "I will respond as requested by the user.\n"
            "I will now inspect the project.\n\n"
            "**Resposta final**\n- ok"
        )
        gatekeeper.save_members({
            "members": [{
                "telegram_id": self.dev.id,
                "telegram_username": self.dev.username,
                "alias": "agent_dev",
                "agent": "codex",
                "scope": self.config["scope"],
                "cargo": "DEV",
                "token_delegation": False,
                "invited_by": "owner",
                "invite_token": "herd-test",
                "registered_at": "now",
                "online": True,
            }]
        })
        context = FakeContext(
            self.bot,
            bot_data={
                "config": self.config,
                "herd_path": Path(self.config["scope"]) / ".herd",
            },
        )
        message = FakeMessage(
            text="@agent_dev responda",
            chat=self.group_chat,
            from_user=self.dev,
        )

        await bridge.handle_message(FakeUpdate(message), context)

        sent = self.bot.sent_messages[-1]
        self.assertTrue(self.bot.chat_actions)
        self.assertEqual(self.bot.chat_actions[0]["action"], "typing")
        self.assertNotIn("I will respond", sent["text"])
        self.assertNotIn("I will now inspect", sent["text"])
        self.assertIn("<b>Resposta final</b>", sent["text"])
        self.assertIn("• ok", sent["text"])

    async def test_bridge_uses_thread_and_temporary_notice_for_slow_responses(self):
        original_call_agent = bridge.call_agent
        original_delay = bridge.PROCESSING_NOTICE_DELAY_SECONDS

        def slow_call_agent(config, message_text, herd_path, context_lines=20):
            import time

            time.sleep(0.08)
            return "ok final"

        bridge.call_agent = slow_call_agent
        bridge.PROCESSING_NOTICE_DELAY_SECONDS = 0.01
        gatekeeper.save_members({
            "members": [{
                "telegram_id": self.dev.id,
                "telegram_username": self.dev.username,
                "alias": "agent_dev",
                "agent": "codex",
                "scope": self.config["scope"],
                "cargo": "DEV",
                "token_delegation": False,
                "invited_by": "owner",
                "invite_token": "herd-test",
                "registered_at": "now",
                "online": True,
            }]
        })
        context = FakeContext(
            self.bot,
            bot_data={
                "config": self.config,
                "herd_path": Path(self.config["scope"]) / ".herd",
            },
        )
        message = FakeMessage(
            text="@agent_dev responda devagar",
            chat=self.group_chat,
            from_user=self.dev,
            message_thread_id=88,
        )

        try:
            await bridge.handle_message(FakeUpdate(message), context)
        finally:
            bridge.call_agent = original_call_agent
            bridge.PROCESSING_NOTICE_DELAY_SECONDS = original_delay

        self.assertEqual(self.bot.sent_messages[0]["text"], "⏳")
        self.assertEqual(self.bot.sent_messages[0]["kwargs"]["message_thread_id"], 88)
        self.assertEqual(self.bot.sent_messages[0]["kwargs"]["reply_to_message_id"], message.message_id)
        self.assertEqual(self.bot.sent_messages[-1]["kwargs"]["message_thread_id"], 88)
        self.assertTrue(self.bot.deleted_messages)

    async def test_invite_rejects_scope_outside_allowed_scope(self):
        self.config["cargo"] = "OWNER"
        self.config["alias"] = "agent_owner"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [{
                "telegram_id": self.owner.id,
                "telegram_username": self.owner.username,
                "alias": "agent_owner",
                "agent": "gemini",
                "scope": self.config["scope"],
                "cargo": "OWNER",
                "token_delegation": True,
                "invited_by": "system",
                "invite_token": "bootstrap",
                "registered_at": "now",
                "online": True,
            }]
        })

        outside_scope = Path(self.tmpdir.name) / "outside-team-scope"
        outside_scope.mkdir(parents=True, exist_ok=True)
        invite_message = FakeMessage(
            text=f"/invite @agent_dev role=DEV scope={outside_scope}",
            chat=self.group_chat,
            from_user=self.owner,
        )

        await gatekeeper.dispatch_command(FakeUpdate(invite_message), FakeContext(self.bot))

        self.assertTrue(invite_message.replies)
        self.assertIn("allowed scope", invite_message.replies[-1]["text"])

    async def test_shared_telegram_account_uses_highest_role_for_invites(self):
        self.config["cargo"] = "LEAD"
        self.config["alias"] = "agent_lead"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_dev",
                    "agent": "codex",
                    "scope": self.config["scope"],
                    "cargo": "DEV",
                    "token_delegation": False,
                    "invited_by": "system",
                    "invite_token": "bootstrap",
                    "registered_at": "now",
                    "online": True,
                },
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_lead",
                    "agent": "codex",
                    "scope": self.config["scope"],
                    "cargo": "LEAD",
                    "token_delegation": True,
                    "invited_by": "system",
                    "invite_token": "bootstrap",
                    "registered_at": "now",
                    "online": True,
                },
            ]
        })

        invite_message = FakeMessage(
            text=f"/invite @agent_new role=DEV scope={self.scope}",
            chat=self.group_chat,
            from_user=self.owner,
        )

        await gatekeeper.dispatch_command(FakeUpdate(invite_message), FakeContext(self.bot))

        self.assertTrue(invite_message.replies)
        self.assertIn("Token generated", invite_message.replies[-1]["text"])
        self.assertEqual(len(gatekeeper.load_tokens()["tokens"]), 1)

    async def test_scope_command_updates_only_runtime_alias_for_shared_account(self):
        self.config["cargo"] = "OWNER"
        self.config["alias"] = "agent_owner"
        self.config["telegram_id"] = self.owner.id
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_owner",
                    "agent": "gemini",
                    "scope": self.config["scope"],
                    "cargo": "OWNER",
                    "token_delegation": True,
                    "invited_by": "system",
                    "invite_token": "bootstrap",
                    "registered_at": "now",
                    "online": True,
                },
                {
                    "telegram_id": self.owner.id,
                    "telegram_username": self.owner.username,
                    "alias": "agent_qa",
                    "agent": "codex",
                    "scope": "/unchanged-scope",
                    "cargo": "QA",
                    "token_delegation": False,
                    "invited_by": "agent_owner",
                    "invite_token": "herd-test",
                    "registered_at": "now",
                    "online": True,
                },
            ]
        })

        env_path = Path(self.tmpdir.name) / ".env"
        os.environ["ENV_PATH"] = str(env_path)

        live_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        bot_data = {
            "config": live_config,
            "herd_path": Path(self.config["scope"]) / ".herd",
        }
        bridge_state = {
            "config_path": Path("config.json"),
            "config_ref": live_config,
            "bot_data_ref": bot_data,
            "status": {"online": True, "setup_complete": True, "alias": "agent_owner", "scope": self.config["scope"]},
            "herd_path": str(Path(self.config["scope"]) / ".herd"),
        }
        bot_data["bridge_state"] = bridge_state

        new_scope = Path(self.tmpdir.name) / "shared-account-scope"
        new_scope.mkdir(parents=True, exist_ok=True)
        message = FakeMessage(f"/scope {new_scope}", self.group_chat, self.owner)

        await gatekeeper.dispatch_command(FakeUpdate(message), FakeContext(self.bot, bot_data=bot_data))

        members = gatekeeper.load_members()["members"]
        owner_entry = next(member for member in members if member["alias"] == "agent_owner")
        qa_entry = next(member for member in members if member["alias"] == "agent_qa")
        self.assertEqual(owner_entry["scope"], str(new_scope))
        self.assertEqual(qa_entry["scope"], "/unchanged-scope")

    async def test_cron_allows_shared_account_to_schedule_for_owned_alias(self):
        gatekeeper.save_members({
            "members": [
                {
                    "telegram_id": self.dev.id,
                    "telegram_username": self.dev.username,
                    "alias": "agent_dev",
                    "agent": "codex",
                    "scope": self.config["scope"],
                    "cargo": "DEV",
                    "token_delegation": False,
                    "invited_by": "owner",
                    "invite_token": "herd-test",
                    "registered_at": "now",
                    "online": True,
                },
                {
                    "telegram_id": self.dev.id,
                    "telegram_username": self.dev.username,
                    "alias": "agent_qa",
                    "agent": "codex",
                    "scope": self.config["scope"],
                    "cargo": "QA",
                    "token_delegation": False,
                    "invited_by": "owner",
                    "invite_token": "herd-test",
                    "registered_at": "now",
                    "online": True,
                },
            ]
        })

        add_message = FakeMessage(
            text='/cron add @agent_qa "run shared audit" * * * * *',
            chat=self.group_chat,
            from_user=self.dev,
        )

        await cron_manager.handle_cron(FakeUpdate(add_message), FakeContext(self.bot))

        self.assertTrue(add_message.replies)
        self.assertIn("Task Scheduled", add_message.replies[-1]["text"])
        tasks = cron_manager.load_tasks()["tasks"]
        self.assertEqual(tasks[0]["alias"], "agent_qa")

    async def test_bridge_report_only_writes_markdown_report(self):
        self.config["allow_project_changes"] = False
        Path("config.json").write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        gatekeeper.save_members({
            "members": [{
                "telegram_id": self.dev.id,
                "telegram_username": self.dev.username,
                "alias": "agent_dev",
                "agent": "codex",
                "scope": self.config["scope"],
                "cargo": "DEV",
                "token_delegation": False,
                "invited_by": "owner",
                "invite_token": "herd-test",
                "registered_at": "now",
                "online": True,
            }]
        })

        original_report_call = bridge.call_agent_report_only
        bridge.call_agent_report_only = lambda config, message_text, herd_path, context_lines=20: (
            "## Summary\n\nSem permissão para alterar o projeto.\n\n## Proposed Changes\n\n- Ajustar auth\n- Criar testes"
        )
        try:
            context = FakeContext(
                self.bot,
                bot_data={
                    "config": self.config,
                    "herd_path": Path(self.config["scope"]) / ".herd",
                },
            )
            message = FakeMessage(
                text="@agent_dev revise esse fluxo sem escrever",
                chat=self.group_chat,
                from_user=self.dev,
            )

            await bridge.handle_message(FakeUpdate(message), context)
        finally:
            bridge.call_agent_report_only = original_report_call

        reports = sorted((Path(self.config["scope"]) / ".herd" / "outputs" / "reports").glob("*.md"))
        self.assertEqual(len(reports), 1)
        report_text = reports[0].read_text(encoding="utf-8")
        self.assertIn("# Herd Report", report_text)
        self.assertIn("## Request", report_text)
        self.assertIn("Sem permissão para alterar o projeto.", report_text)

        sent = self.bot.sent_messages[-1]
        self.assertIn("Project changes are disabled", sent["text"])
        self.assertIn(".md", sent["text"])

    def test_update_runtime_access_mode_updates_config_and_env(self):
        env_path = Path(self.tmpdir.name) / ".env"
        os.environ["ENV_PATH"] = str(env_path)

        live_config = dict(self.config)
        bot_data = {
            "config": live_config,
            "herd_path": Path(self.config["scope"]) / ".herd",
        }
        bridge_state = {
            "config_path": Path("config.json"),
            "config_ref": live_config,
            "bot_data_ref": bot_data,
            "status": {"online": True, "setup_complete": True, "alias": self.config["alias"], "scope": self.config["scope"]},
            "herd_path": str(Path(self.config["scope"]) / ".herd"),
        }

        updated = bridge.update_runtime_access_mode(False, bridge_state, sync_env=True)

        saved_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertFalse(saved_config["allow_project_changes"])
        self.assertFalse(live_config["allow_project_changes"])
        self.assertEqual(updated["access_mode"], "report-only")
        self.assertFalse(bridge_state["status"]["allow_project_changes"])

        env_contents = env_path.read_text(encoding="utf-8")
        self.assertIn("HERD_ALLOW_PROJECT_CHANGES=false", env_contents)

        agent_metadata = json.loads((Path(self.config["scope"]) / ".herd" / "agent.json").read_text(encoding="utf-8"))
        self.assertFalse(agent_metadata["allow_project_changes"])
        self.assertEqual(agent_metadata["access_mode"], "report-only")

    async def _bootstrap_owner(self):
        join_message = FakeMessage(
            text=None,
            chat=self.group_chat,
            from_user=self.owner,
            new_chat_members=[self.owner],
        )
        await gatekeeper.handle_new_member(FakeUpdate(join_message), FakeContext(self.bot))

        members = gatekeeper.load_members()["members"]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["cargo"], "OWNER")

    async def _invite_developer(self) -> str:
        invite_message = FakeMessage(
            text=f"/invite @agent_dev role=DEV scope={self.scope}",
            chat=self.group_chat,
            from_user=self.owner,
        )
        await gatekeeper.dispatch_command(FakeUpdate(invite_message), FakeContext(self.bot))

        self.assertTrue(invite_message.replies)
        self.assertIn("Token generated", invite_message.replies[-1]["text"])

        tokens = gatekeeper.load_tokens()["tokens"]
        self.assertEqual(len(tokens), 1)
        return tokens[0]["token"]

    async def _complete_onboarding(self, token: str):
        for text in [token, "2", "agent_dev"]:
            dm_message = FakeMessage(text=text, chat=self.dm_chat, from_user=self.dev)
            await gatekeeper.handle_dm(FakeUpdate(dm_message), FakeContext(self.bot))

        members = gatekeeper.load_members()["members"]
        self.assertEqual(len(members), 2)
        self.assertEqual(members[1]["alias"], "agent_dev")
        self.assertEqual(members[1]["agent"], "codex")

    async def _exercise_bridge(self):
        context = FakeContext(
            self.bot,
            bot_data={
                "config": self.config,
                "herd_path": Path(self.config["scope"]) / ".herd",
            },
        )
        message = FakeMessage(
            text="@agent_dev revise esse fluxo",
            chat=self.group_chat,
            from_user=self.dev,
        )
        await bridge.handle_message(FakeUpdate(message), context)

        today = bridge.datetime.now(bridge.timezone.utc).strftime("%Y-%m-%d")
        log_path = Path(self.config["scope"]) / ".herd" / "memory" / "conversations" / f"{today}.jsonl"
        self.assertTrue(log_path.exists())
        log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(log_lines), 2)

    async def _exercise_cron(self):
        add_message = FakeMessage(
            text='/cron add @agent_dev "run local smoke" * * * * *',
            chat=self.group_chat,
            from_user=self.dev,
        )
        await cron_manager.handle_cron(FakeUpdate(add_message), FakeContext(self.bot))

        tasks = cron_manager.load_tasks()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["alias"], "agent_dev")

        await cron_manager.cron_tick(FakeContext(self.bot))

        tasks_after = cron_manager.load_tasks()["tasks"]
        self.assertEqual(tasks_after[0]["run_count"], 1)
        self.assertIsNotNone(tasks_after[0]["last_run"])


if __name__ == "__main__":
    unittest.main()
