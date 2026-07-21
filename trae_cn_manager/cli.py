"""TCN command-line interface (Typer + Rich)."""
from __future__ import annotations

import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from . import db as tcn_db
from .config import get_app_data_dir, get_trae_cn_data_dir, get_proxy, is_windows
from .models import Account
from .process_ctl import DefaultProcessController, get_trae_cn_exe_path, set_trae_cn_exe_path
from .switcher import Switcher

app = typer.Typer(
    name="tcn",
    help="Trae CN Manager — register, switch, and inspect Trae CN accounts.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_account(identifier: str) -> Account | None:
    """Look up account by id prefix or email."""
    for acc in tcn_db.list_accounts(only_active=True):
        if acc.id.startswith(identifier) or acc.email == identifier or acc.phone == identifier:
            return acc
    return None


def _current_account() -> Account | None:
    cid = tcn_db.get_current_account_id()
    return tcn_db.get_account(cid) if cid else None


# ---------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Show version."""
    console.print(f"tcn {__version__}")


@app.command(name="list")
def list_accounts(
    only_active: bool = typer.Option(False, "--active", help="only enabled accounts"),
) -> None:
    """List registered accounts."""
    accs = tcn_db.list_accounts(only_active=only_active)
    if not accs:
        console.print("[dim]no accounts registered. Run[/dim] tcn register")
        return
    current_id = tcn_db.get_current_account_id()
    t = Table("current", "id", "phone", "name", "region", "plan", "status", "user_id")
    for a in accs:
        cur = "*" if a.id == current_id else ""
        t.add_row(
            cur, a.id[:8], a.phone or a.email, a.name, a.region, a.plan_type,
            a.status, a.user_id[:8] if a.user_id else "",
        )
    console.print(t)


@app.command()
def current() -> None:
    """Show the account currently driving Trae CN."""
    a = _current_account()
    if not a:
        console.print("[dim]no current account[/dim]")
        return
    console.print(f"[bold]{a.phone or a.email}[/bold]  ({a.name})")
    console.print(f"  region     : {a.region}")
    console.print(f"  plan       : {a.plan_type}")
    console.print(f"  user_id    : {a.user_id}")
    console.print(f"  machine_id : {a.machine_id or '(none)'}")
    console.print(f"  status     : {a.status}")


@app.command()
def switch(
    account_id: str = typer.Argument(..., help="account id (or unique prefix)"),
    launch: bool = typer.Option(True, "--launch/--no-launch", help="launch Trae CN after switch"),
    reset_registry: bool = typer.Option(
        False, "--reset-registry", help="reset Windows MachineGuid"
    ),
) -> None:
    """Switch to a different account."""
    acc = _find_account(account_id)
    if not acc:
        console.print(f"[red]account not found:[/red] {account_id}")
        raise typer.Exit(1)
    sw = Switcher()
    try:
        result = sw.switch_to_account(acc, launch=launch, reset_registry=reset_registry)
        console.print(f"[green]✓[/green] switched to [bold]{acc.phone or acc.email}[/bold]")
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1)


@app.command()
def register(
    count: int = typer.Argument(1, help="number of accounts to register"),
    concurrency: int = typer.Option(1, "-c", "--concurrency", help="max concurrency"),
    headed: bool = typer.Option(False, "--headed", help="show browser window (for captcha solving)"),
    no_persist: bool = typer.Option(False, "--no-persist", help="don't save to database"),
) -> None:
    """Register new Trae CN account(s) via SMS."""
    from .register import register_batch_sync

    total = min(max(count, 1), 50)
    console.print(f"Registering [bold]{total}[/bold] account(s)...")
    results = register_batch_sync(
        total=total,
        concurrency=concurrency,
        headed=headed,
        persist=not no_persist,
    )
    success = sum(1 for r in results if r.success)
    console.print(f"\n[green]✓ {success}/{total} registered successfully[/green]")
    for r in results:
        status = "[green]✓[/green]" if r.success else "[red]✗[/red]"
        detail = f"{r.phone} → {r.user_id}" if r.success else r.error
        console.print(f"  {status} {detail}")


@app.command()
def usage(
    account_id: Optional[str] = typer.Argument(None, help="account id (default: current)"),
) -> None:
    """Query usage quota."""
    from .trae_api import TraeCnApiClient, parse_jwt
    from .vault import decrypt_obj

    acc = _find_account(account_id) if account_id else _current_account()
    if not acc:
        console.print("[red]no account specified and no current account[/red]")
        raise typer.Exit(1)

    secrets = decrypt_obj(acc.secrets_blob)
    token = secrets.get("token", "")
    if not token:
        console.print(f"[red]account {acc.phone or acc.email} has no token[/red]")
        raise typer.Exit(1)

    api = TraeCnApiClient()
    summary = api.get_usage_summary(token)
    if not summary:
        console.print("[yellow]could not fetch usage data[/yellow]")
        return

    t = Table("metric", "limit", "used", "left")
    t.add_row("plan", summary.plan_type, "", "")
    t.add_row("fast requests", str(summary.fast_request_limit), str(summary.fast_request_used), str(summary.fast_request_left))
    t.add_row("slow requests", str(summary.slow_request_limit), str(summary.slow_request_used), str(summary.slow_request_left))
    t.add_row("advanced models", str(summary.advanced_model_limit), str(summary.advanced_model_used), str(summary.advanced_model_left))
    t.add_row("autocomplete", str(summary.autocomplete_limit), str(summary.autocomplete_used), str(summary.autocomplete_left))
    console.print(t)


@app.command()
def capture(
    name: Optional[str] = typer.Option(None, "--name", help="friendly name"),
    phone: Optional[str] = typer.Option(None, "--phone", help="phone number"),
) -> None:
    """Capture the current Trae CN session into the local store."""
    sw = Switcher()
    acc = sw.capture_current(name=name or "", phone=phone or "")
    if acc:
        console.print(f"[green]✓[/green] captured [bold]{acc.email or acc.phone}[/bold] (id={acc.id[:8]})")
    else:
        console.print("[red]✗[/red] could not capture — is Trae CN logged in?")


@app.command()
def clear(
    launch: bool = typer.Option(False, "--launch", help="restart Trae CN after clearing"),
) -> None:
    """Reset Trae CN to logged-out state."""
    sw = Switcher()
    sw.clear_login_state(launch=launch)
    console.print("[green]✓[/green] login state cleared")


@app.command()
def delete(
    account_id: str = typer.Argument(..., help="account id (or unique prefix)"),
) -> None:
    """Delete an account from local database."""
    acc = _find_account(account_id)
    if not acc:
        console.print(f"[red]account not found:[/red] {account_id}")
        raise typer.Exit(1)
    cid = tcn_db.get_current_account_id()
    if acc.id == cid:
        tcn_db.set_current_account(None)
    tcn_db.delete_account(acc.id)
    console.print(f"[green]✓[/green] deleted [bold]{acc.phone or acc.email}[/bold]")


@app.command(name="set-path")
def set_path(
    path: str = typer.Argument(..., help="full path to Trae CN executable"),
) -> None:
    """Set Trae CN executable path."""
    set_trae_cn_exe_path(path)
    console.print(f"[green]✓[/green] path set to [bold]{path}[/bold]")


@app.command()
def path() -> None:
    """Show configured Trae CN executable path."""
    p = get_trae_cn_exe_path()
    if p:
        console.print(p)
    else:
        console.print("[red]Trae CN executable not configured[/red]")
        console.print("Set it via [bold]tcn set-path <path>[/bold]")


@app.command()
def info() -> None:
    """Show environment info."""
    console.print(f"tcn version     : {__version__}")
    console.print(f"app data dir    : {get_app_data_dir()}")
    console.print(f"trae CN data dir: {get_trae_cn_data_dir()}")
    console.print(f"trae CN exe     : {get_trae_cn_exe_path() or '(not set)'}")
    console.print(f"proxy           : {get_proxy() or '(none)'}")
    console.print(f"OS              : {'Windows' if is_windows() else 'macOS'}")
    console.print(f"accounts in DB  : {len(tcn_db.list_accounts())}")
    cid = tcn_db.get_current_account_id()
    console.print(f"current account : {cid or '(none)'}")
