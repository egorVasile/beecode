import asyncio

from beeagent.config.schema import BeeConfig


def test_tui_smoke():
    """Mount the full-screen app headless and exercise the main paths."""
    from beeagent.ui.tui import BeeCodeApp
    from textual.widgets import Input, RichLog

    async def go():
        app = BeeCodeApp(config=BeeConfig())
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.query_one("#prompt", Input) is not None
            assert app.query_one("#log", RichLog) is not None

            # sidebar filtering and bee animation must not raise
            app._populate_commands("mo")
            app._animate_bee()
            await pilot.pause()

            # running a slash command writes output to the log
            app.prompt.value = "/about"
            app._submit()
            await pilot.pause()
            assert len(app.chatlog.lines) > 0

            # chrome actions
            app.action_toggle_sidebar()
            app._update_status()
            await pilot.pause()

    asyncio.run(go())


def test_tui_title_and_bindings():
    from beeagent.ui.tui import BeeCodeApp
    app = BeeCodeApp(config=BeeConfig())
    assert app.TITLE == "BeeCode"
    keys = {b.key for b in app.BINDINGS}
    assert "ctrl+q" in keys
    assert "ctrl+b" in keys


def test_the_sidebar_yields_the_screen_to_a_phone():
    """40 columns of command list on a 60-column terminal is half the app.

    The first version read `screen.width`, which is None until a layout pass has
    happened, so the fallback made every terminal look narrow and the sidebar
    vanished on desktop too. `app.size` is the value that is actually there.
    """
    import asyncio

    from beeagent.config.schema import BeeConfig
    from beeagent.ui.tui import BeeCodeApp

    async def hidden_at(width):
        app = BeeCodeApp(config=BeeConfig())
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause(0.3)
            return app.query_one("#sidebar").has_class("hidden")

    async def go():
        return (await hidden_at(60), await hidden_at(130))

    narrow, wide = asyncio.run(go())
    assert narrow is True, "a phone should get the whole width for the chat"
    assert wide is False, "a desktop must keep the sidebar it was designed for"
