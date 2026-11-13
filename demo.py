"""A compiled LangGraph driving two isolated e2e sessions over public MCP stdio."""

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import TypedDict

import psutil
from langgraph.graph import END, START, StateGraph
from mcp import Client, StdioServerParameters, stdio_client

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

ROOT = Path(__file__).resolve().parent


class DeliberateFailure(RuntimeError):
    """The example's expected exception after both actors have worked."""


class State(TypedDict):
    """Only serializable IDs and scenario data enter the graph state."""

    scenario: str
    sessions: dict[str, str]
    checked: bool


class Scenario:
    """Own the transport, app and two logical session clients for one graph run."""

    def __init__(self, name: str, output: Path):
        self.name = name
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, str] = {}
        self.calls: list[dict] = []
        self.processes: dict[int, psutil.Process] = {}
        self.listeners: set[tuple[str, int]] = set()
        self.in_flight = asyncio.Event()
        self.client: Client
        self.url = ""
        self.port = 0

    def remember_children(self):
        """Retain process identity (including creation time) across reparenting."""
        for process in psutil.Process().children(recursive=True):
            try:
                process.create_time()
                self.processes.setdefault(process.pid, process)
            except psutil.NoSuchProcess:
                pass

    def remember_ports(self):
        """Record listening TCP ports of live owned processes, without global scans."""
        self.remember_children()
        for process in self.processes.values():
            try:
                for connection in process.net_connections(kind="inet"):
                    if connection.status == psutil.CONN_LISTEN:
                        host = connection.laddr.ip
                        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
                        self.listeners.add((host, connection.laddr.port))
            except psutil.NoSuchProcess:
                pass

    def remaining_processes(self):
        """Check the original process identities, guarding against PID reuse."""
        return [
            process.pid for process in self.processes.values() if process.is_running()
        ]

    async def monitor(self):
        """Observe only this Python runner's descendant processes."""
        while True:
            self.remember_children()
            await asyncio.sleep(0.02)

    async def request(self, name: str, arguments: dict, *, error: str | None = None):
        """Send an actual tools/call and preserve its routing and text result."""
        record = {"tool": name, "arguments": arguments, "started": time.monotonic()}
        self.calls.append(record)
        try:
            result = await self.client.call_tool(name, arguments)
            text = "\n".join(
                block.text for block in result.content if block.type == "text"
            )
            record.update(text=text, is_error=result.is_error)
            for block in result.content:
                if block.type == "image":
                    path = self.output / f"screen-{len(self.calls)}.png"
                    path.write_bytes(base64.b64decode(block.data))
                    record["image"] = str(path)
            if error is not None:
                if not result.is_error or error not in text:
                    raise AssertionError(f"Expected {error}, got {text}")
            elif result.is_error:
                raise AssertionError(f"{name}: {text}")
            return text
        except asyncio.CancelledError:
            record["cancelled"] = True
            raise
        finally:
            record["finished"] = time.monotonic()

    async def call(self, state: State, actor: str, tool: str, args: dict | None = None):
        """Route the named actor through the session ID held in graph state."""
        return await self.request(
            "call",
            {"session": state["sessions"][actor], "tool": tool, "args": args or {}},
        )

    async def code(self, state: State, actor: str, code: str, timeout: int = 5000):
        """Run the server's public, parsed test-statement grammar."""
        return await self.call(
            state, actor, "run_code", {"code": code, "timeout": timeout}
        )

    async def open(self, _state: State):
        """Open A and B before either actor changes the browser."""
        for actor in ("A", "B"):
            text = await self.request(
                "open_session", {"engine": "@e2e-dev/web", "app": {"url": self.url}}
            )
            match = re.search(r"^Session (\S+) open", text)
            if match is None:
                raise AssertionError(f"No session ID in open_session: {text}")
            self.sessions[actor] = match[1]
        self.remember_ports()
        if self.sessions["A"] == self.sessions["B"]:
            raise AssertionError("A and B have the same session ID")
        await self.request("call", {"tool": "observe"}, error="SESSION_REQUIRED")
        for session in self.sessions.values():
            await self.request("tools", {"session": session, "tool": "run_code"})
        return {"sessions": dict(self.sessions)}

    async def task_a(self, state: State):
        """A writes a note and counts two requests."""
        await self.code(
            state,
            "A",
            "await screen.getByLabel('Citizen note').fill('ALPHA-SYNTHETIC'); "
            "await screen.getByRole('button', {name: 'Save note', exact: true}).click(); "
            "await screen.getByRole('button', {name: 'Count request', exact: true}).click(); "
            "await screen.getByRole('button', {name: 'Count request', exact: true}).click();",
        )
        return {}

    async def b_still_empty(self, state: State):
        """A's localStorage and actions must not have reached B."""
        await self.code(
            state,
            "B",
            "await expect(screen.getByRole('status')).toHaveText('Note: empty | Requests: 0'); "
            "await expect(screen.getByLabel('Citizen note')).toHaveValue('');",
        )
        return {}

    async def task_b(self, state: State):
        """B counts one request and leaves its note empty."""
        await self.code(
            state,
            "B",
            "await screen.getByRole('button', {name: 'Count request', exact: true}).click();",
        )
        return {}

    async def verify(self, state: State):
        """Reload each browser to prove localStorage isolation in both directions."""
        for actor, expected in (
            ("A", "Note: ALPHA-SYNTHETIC | Requests: 2"),
            ("B", "Note: empty | Requests: 1"),
        ):
            await self.code(
                state,
                actor,
                f"await app.open({json.dumps(self.url)}); "
                f"await expect(screen.getByRole('status')).toHaveText({json.dumps(expected)});",
            )
            await self.call(state, actor, "observe")
            await self.call(state, actor, "screenshot")
        return {"checked": True}

    async def finish(self, state: State):
        """Exercise success, a graph exception, or cancellation of an MCP call."""
        if state["scenario"] == "exception":
            raise DeliberateFailure("intentional failure after isolation assertions")
        if state["scenario"] == "cancel":
            self.in_flight.set()
            await self.code(
                state,
                "A",
                "await expect(screen.getByRole('status')).toHaveText('NEVER-APPEARS');",
                timeout=30000,
            )
            raise AssertionError("The cancellation probe unexpectedly completed")
        return {}

    def graph(self):
        """Compile real LangGraph nodes, keeping resource ownership outside the graph."""
        builder = StateGraph(State)
        nodes = [
            ("open", self.open),
            ("task_a", self.task_a),
            ("b_still_empty", self.b_still_empty),
            ("task_b", self.task_b),
            ("verify", self.verify),
            ("finish", self.finish),
        ]
        previous = START
        for name, node in nodes:
            builder.add_node(name, node)
            builder.add_edge(previous, name)
            previous = name
        builder.add_edge(previous, END)
        return builder.compile()

    async def drive(self):
        """Await the graph; cancel only after its final MCP call has started."""
        task = asyncio.create_task(
            self.graph().ainvoke(
                {"scenario": self.name, "sessions": {}, "checked": False}
            )
        )
        try:
            if self.name == "cancel":
                ready = asyncio.create_task(self.in_flight.wait())
                try:
                    done, _pending = await asyncio.wait(
                        [task, ready], return_when=asyncio.FIRST_COMPLETED, timeout=90
                    )
                    if not done:
                        raise TimeoutError("Graph did not reach cancellation probe")
                    if task in done:
                        await task
                        raise AssertionError("Graph ended before cancellation")
                    await asyncio.sleep(0.25)
                    task.cancel()
                finally:
                    ready.cancel()
                    await asyncio.gather(ready, return_exceptions=True)
            result = await task
            if self.name != "normal" or not result["checked"]:
                raise AssertionError("Unexpected graph completion")
            return "completed"
        except DeliberateFailure:
            if self.name != "exception":
                raise
            return "deliberate_exception"
        except asyncio.CancelledError:
            if self.name != "cancel" or not task.cancelled():
                raise
            return "cancelled_in_flight"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close_sessions(self):
        """Try every explicit close even when an earlier teardown fails."""
        failures = []
        for actor, session in self.sessions.items():
            try:
                await asyncio.wait_for(
                    self.request("close_session", {"session": session}), timeout=40
                )
                await self.request(
                    "call", {"session": session, "tool": "observe"}, error="NO_SESSION"
                )
            except Exception as exc:  # noqa: BLE001 -- Try both closes before raising teardown failures.
                failures.append(f"{actor}: {exc}")
        if failures:
            raise AssertionError("Session teardown failed: " + "; ".join(failures))

    async def run(self):
        """Always close sessions, stdio, the owned app, and record cleanup evidence."""
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node 24+ must be on PATH")
        # Neither application receives credentials or the caller's full environment.
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
            if key in os.environ
        }
        env["E2E_TELEMETRY_DISABLED"] = "1"
        app = await asyncio.create_subprocess_exec(
            node, str(ROOT / "app.mjs"), env=env, stdout=asyncio.subprocess.PIPE
        )
        monitor = asyncio.create_task(self.monitor())
        outcome = "failed"
        validated = False
        try:
            line = await asyncio.wait_for(app.stdout.readline(), timeout=10)
            address = json.loads(line)
            self.url, self.port = address["url"], address["port"]
            params = StdioServerParameters(
                command=node,
                args=[
                    str(ROOT / "node_modules/e2e/dist/cli/bin.js"),
                    "mcp",
                    "--max-sessions",
                    "2",
                ],
                cwd=ROOT,
                env=env,
            )
            with (self.output / "mcp.stderr.log").open("w") as log:
                async with Client(
                    stdio_client(params, errlog=log), read_timeout_seconds=90
                ) as client:
                    self.client = client
                    catalog = await client.list_tools()
                    if not {"open_session", "tools", "call", "close_session"} <= {
                        tool.name for tool in catalog.tools
                    }:
                        raise AssertionError("Unexpected MCP catalog")
                    try:
                        outcome = await self.drive()
                        await self.code(
                            {
                                "scenario": self.name,
                                "sessions": dict(self.sessions),
                                "checked": True,
                            },
                            "B",
                            "await expect(screen.getByRole('status')).toHaveText('Note: empty | Requests: 1');",
                        )
                    finally:
                        self.remember_ports()
                        await self.close_sessions()
            validated = True
        finally:
            self.remember_children()
            if app.returncode is None:
                app.terminate()
            try:
                await asyncio.wait_for(app.wait(), timeout=10)
            except TimeoutError:
                app.kill()  # This handle is exclusively the subprocess this fixture started.
                await app.wait()
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            for _ in range(50):
                live = self.remaining_processes()
                if not live:
                    break
                await asyncio.sleep(0.1)
            open_listeners = []
            listeners = self.listeners | (
                {("127.0.0.1", self.port)} if self.port else set()
            )
            for host, port in sorted(listeners):
                try:
                    with socket.create_connection((host, port), timeout=0.25):
                        open_listeners.append((host, port))
                except OSError:
                    pass
            open_ports = sorted({port for _host, port in open_listeners})
            port_open = ("127.0.0.1", self.port) in open_listeners
            receipt = {
                "scenario": self.name,
                "outcome": outcome
                if validated and not live and not open_ports
                else "failed",
                "sessions": self.sessions,
                "app_pid": app.pid,
                "app_port": self.port,
                "observed_pids": sorted(self.processes),
                "observed_tcp_listeners": sorted(listeners),
                "observed_tcp_ports": sorted({port for _host, port in listeners}),
                "remaining_tcp_ports": open_ports,
                "remaining_pids": live,
                "app_port_open": port_open,
                "calls": self.calls,
            }
            (self.output / "receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
            if live or open_ports:
                raise AssertionError(
                    f"Cleanup leak: pids={live}, open_ports={open_ports}"
                )
        print(
            f"{self.name}: {outcome}; sessions={self.sessions}; remaining_pids=[]; app_port_open=false"
        )
        return receipt


async def main():
    """Run requested scenarios without an LLM, accounts, or remote services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=("normal", "exception", "cancel", "all"), default="all"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    for name in (
        ("normal", "exception", "cancel")
        if args.scenario == "all"
        else (args.scenario,)
    ):
        await Scenario(name, args.output / name).run()


if __name__ == "__main__":
    asyncio.run(main())
