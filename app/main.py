"""FastAPIアプリ本体(ルーティングと画面の組み立て)。"""
import threading
import sqlite3
import mimetypes
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, TypeAlias
from datetime import date
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import blockers, clock, daily, db as database, reports, tasks
from app.version import compute_app_version
from app.ai import actions as ai_actions

BASE_DIR = Path(__file__).resolve().parent
LOCAL_HOSTNAMES = ("127.0.0.1", "localhost")
WEEKDAY_NAMES = "月火水木金土日"
# 「アプリとしてインストール」ボタンを出すか。ローカル(127.0.0.1)で入れると、Azure に移ったときに入れ直しになるため、
# Azure に載せるまでは出さない(2026-10-28 決定)。小さく使う目的はミニ表示で果たす。
SHOW_INSTALL_BUTTON = False


def format_duration(minutes: int | None) -> str:
    if not minutes:
        return "0分"
    hours, rest_minutes = divmod(minutes, 60)
    if hours and rest_minutes:
        return f"{hours}時間{rest_minutes}分"
    return f"{hours}時間" if hours else f"{rest_minutes}分"


def format_clock_time(iso_text: str | None) -> str:
    return iso_text[11:16] if iso_text else ""


def format_long_date(target_date: date) -> str:
    return f"{target_date.year}年{target_date.month}月{target_date.day}日({WEEKDAY_NAMES[target_date.weekday()]})"


def format_today() -> str:
    return format_long_date(clock.today())


def format_month_day(value: date | str | None) -> str:
    """「10/27(月)」の形にする。"""
    if not value:
        return ""
    target_date = date.fromisoformat(value[:10]) if isinstance(value, str) else value
    return f"{target_date.month}/{target_date.day}({WEEKDAY_NAMES[target_date.weekday()]})"


def parse_report_date(text: str) -> date:
    """日報の日付指定。読めない値や未来の日付は今日として扱う。"""
    try:
        target_date = date.fromisoformat(text)
    except ValueError:
        return clock.today()
    return min(target_date, clock.today())


def get_db(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """要求ごとにDBを開き、成功時は保存、失敗時は取り消して必ず閉じる。"""
    connection = database.connect(request.app.state.db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# 応答を返す前にコミットされるよう scope="function" を指定する
Database: TypeAlias = Annotated[sqlite3.Connection, Depends(get_db, scope="function")]


def view_state(tag: str = "", view: str = "") -> dict:
    """どの画面から操作したか。ミニ表示(view=mini)から押した操作はミニ表示を描き直す。"""
    return {"tag": tag, "view": view if view in ("mini", "archive") else "board"}


ViewState: TypeAlias = Annotated[dict, Depends(view_state)]


def owned_task(task_id: int, db: Database) -> sqlite3.Row | None:
    """操作の対象が固定利用者本人のタスクかを確かめ、他人のタスクを操作させない。"""
    return tasks.get_owned_task(db, database.DEFAULT_USER_ID, task_id)


OwnedTask: TypeAlias = Annotated[sqlite3.Row | None, Depends(owned_task)]


def create_app(db_path: Path | str | None = None) -> FastAPI:
    resolved_db_path = db_path or database.resolve_db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.init_db(resolved_db_path)
        # 他プロセス(MCPサーバー)による更新を検知するための接続。PRAGMA data_version は
        # この接続以外からのコミットで値が変わる。
        app.state.version_connection = database.connect(resolved_db_path)
        yield
        app.state.version_connection.close()

    app = FastAPI(title="ToDoリスト", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = resolved_db_path
    version_lock = threading.Lock()
    # DNSリバインディング対策: ローカルのホスト名以外へのアクセスは拒否する
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTNAMES))
    # 応答を圧縮する(画面の HTML は約 1/10 になる)。App Service の無料プランは 1 日の通信量に上限(165MB)があるため
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    # Windowsのファイル関連付けによらず、JavaScriptとして配信する。
    mimetypes.add_type("text/javascript", ".js")
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters["duration"] = format_duration
    templates.env.filters["clock_time"] = format_clock_time
    templates.env.globals.update(
        STATUS_LABELS=tasks.STATUS_LABELS, LEVEL_LABELS=tasks.LEVEL_LABELS,
        RECURRENCE_LABELS=tasks.RECURRENCE_LABELS,
        WORK_STYLE_LABELS=reports.WORK_STYLE_LABELS, EARLY_LEAVE=reports.EARLY_LEAVE,
        EXCLUSIVE_LEAVE_TYPES=reports.EXCLUSIVE_LEAVE_TYPES, SHOW_INSTALL_BUTTON=SHOW_INSTALL_BUTTON,
    )
    app_version = compute_app_version()
    templates.env.globals["APP_VERSION"] = app_version  # CSS の URL に付けて古いキャッシュを使わせない
    templates.env.filters["hhmm"] = reports.format_hhmm
    templates.env.filters["month_day"] = format_month_day
    user_id = database.DEFAULT_USER_ID

    @app.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        # 他サイトのページからローカルAPIへ更新リクエストを送らせない(CSRF対策)。
        # 送信元(Origin)のホスト名とポートが、このアプリ自身(Host)と一致するときだけ通す。
        # 同じ PC の別のポートで動く別のアプリのページからの送信も断る。
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "許可されていないリクエスト元です"}, status_code=403)
        return await call_next(request)

    def build_view_query(state: dict) -> str:
        params = {}
        if state["tag"]:
            params["tag"] = state["tag"]
        if state.get("view") == "mini":
            params["view"] = "mini"
        return urlencode(params)

    def base_context(request: Request, view: str) -> dict:
        return {"request": request, "view": view, "today_label": format_today(), "today_iso": clock.today().isoformat()}

    def board_context(
        request: Request, db: sqlite3.Connection, state: dict, notice: dict | None = None,
        highlight: dict | None = None,
    ) -> dict:
        report_task_ids = reports.report_task_ids(db, user_id, clock.today())
        return {
            **base_context(request, "board"),
            "view_query": build_view_query(state),
            "notice": notice,
            # 直前の操作で完了/追加されたタスク。そのタスクだけ演出アニメーションを再生する。
            "just_completed_id": (highlight or {}).get("completed"),
            "just_completed_parent_id": (highlight or {}).get("completed_parent"),
            "just_added_id": (highlight or {}).get("added"),
            # 相談したい/返事待ちを押した直後は、その行の一言欄を開いておく
            "just_flagged_id": (highlight or {}).get("flagged"),
            "just_waiting_id": (highlight or {}).get("waiting"),
            "edit_draft": (highlight or {}).get("edit_draft"),
            "board": daily.build_board(db, user_id, report_task_ids, state["tag"] or None),
            "parent_candidates": tasks.list_parent_candidates(db, user_id),
            "report_count": len(report_task_ids),
            "tag_names": tasks.list_tag_names(db, user_id),
            "selected_tag": state["tag"],
        }

    def render_board(
        request: Request, db: sqlite3.Connection, state: dict, notice: dict | None = None,
        highlight: dict | None = None,
    ) -> HTMLResponse:
        """操作後に差し替えるタスク画面の本体(htmx用の部分HTML)を返す。ミニ表示からの操作ならミニ表示を返す。"""
        template_name = "_mini_content.html" if state.get("view") == "mini" else "_board_content.html"
        return templates.TemplateResponse(request, template_name, board_context(request, db, state, notice, highlight))

    # ---- 画面(フルページ) ----

    @app.get("/", response_class=HTMLResponse)
    def board_page(request: Request, db: Database, state: ViewState):
        return templates.TemplateResponse(request, "board.html", board_context(request, db, state))

    @app.get("/mini", response_class=HTMLResponse)
    def mini_page(request: Request, db: Database):
        """今日やるタスクだけの小さな画面。小さな窓で開き、画面の端に置いて使う。"""
        state = {"tag": "", "view": "mini"}
        return templates.TemplateResponse(request, "mini.html", board_context(request, db, state))

    @app.get("/manifest.webmanifest")
    def web_app_manifest():
        """ブラウザの「アプリとしてインストール」に必要な設定。"""
        return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        """インストールしたアプリ用の service worker。アプリ全体を受け持つため、URL の一番上に置く。"""
        return FileResponse(BASE_DIR / "static" / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/tasks")
    def old_tasks_page(tag: str = ""):
        """旧「タスク一覧」画面。タスク画面に統合した。"""
        return RedirectResponse("/" + (f"?{urlencode({'tag': tag})}" if tag else ""), status_code=307)

    @app.get("/reflect")
    def old_reflect_page():
        """旧「今日の振り返り」画面。日報に置き換えた。"""
        return RedirectResponse("/report", status_code=307)

    @app.get("/content", response_class=HTMLResponse)
    def refreshed_content(request: Request, db: Database, state: ViewState):
        """別プロセスによる更新を画面へ反映するための部分HTML。"""
        return render_board(request, db, state)

    @app.get("/api/data-version")
    def data_version(request: Request):
        with version_lock:
            version = request.app.state.version_connection.execute("PRAGMA data_version").fetchone()[0]
        return {"version": version}

    @app.get("/api/app-version")
    def get_app_version():
        """起動プログラムが「動いているサーバーが最新か」を確かめるのに使う。"""
        return {"version": app_version}

    @app.get("/archive", response_class=HTMLResponse)
    def archive_page(request: Request, db: Database, tag: str = ""):
        return templates.TemplateResponse(request, "archive.html", {
            **base_context(request, "archive"),
            "archive_groups": daily.list_archive(db, user_id, tag or None),
            "tag_names": tasks.list_tag_names(db, user_id),
            "selected_tag": tag,
        })

    # ---- 日報 ----

    def report_context(request: Request, db: sqlite3.Connection, report_date: date, **extra) -> dict:
        return {
            **base_context(request, "report"),
            "report": reports.build_report_view(db, user_id, report_date),
            "report_date_label": format_long_date(report_date),
            "is_today": report_date == clock.today(),
            "recent_reports": reports.list_recent_reports(db, user_id),
            "invalid_titles": [],
            "just_submitted": False,
            **extra,
        }

    async def save_report_form(request: Request, db: sqlite3.Connection, report_date: date) -> list[str]:
        """日報フォームの内容を下書きとして保存する。読めない時刻は保存せず、そのタスク名を返す。

        「作業時間」「勤務形態・休暇など」はON/OFFがOFFの間は入力欄自体が画面に無く、フォームに届かない。
        その場合は今の値をそのまま保つ(reports.save_draft が処理する)。
        """
        form = await request.form()
        report = reports.find_report(db, user_id, report_date)
        time_tracking_enabled = bool(report["time_tracking_enabled"]) if report else True
        details_enabled = bool(report["details_enabled"]) if report else True

        minutes_by_task_id: dict[int, int] = {}
        invalid_titles: list[str] = []
        if time_tracking_enabled:
            for key, value in form.multi_items():
                if not key.startswith("time_") or not key[5:].isdigit():
                    continue
                task_id = int(key[5:])
                if not tasks.is_valid_task_id(task_id):
                    continue
                minutes = reports.parse_hhmm(str(value))
                if minutes is None:
                    task = tasks.get_owned_task(db, user_id, task_id)
                    invalid_titles.append(task["title"] if task else str(value))
                else:
                    minutes_by_task_id[task_id] = minutes

        work_style = None
        leave_types = None
        if details_enabled:
            # 出社/テレワークは1つのチェックボックスで届く(チェックあり=テレワーク、なし=出社)。
            # チェックボックスはチェックされていないとフォームに届かないため、値が無ければ出社扱い。
            work_style = "telework" if form.get("work_style") == "telework" else "office"
            # 早退はチェックボックス、時間休・午前休・午後休はどれか1つのラジオボタンで届く
            leave_types = [str(name) for name in [*form.getlist("leave_types"), form.get("partial_leave", "")]]

        reports.save_draft(
            db, user_id, report_date, minutes_by_task_id,
            work_style=work_style,
            leave_types=leave_types,
            comment=str(form.get("comment", "")),
        )
        return invalid_titles

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request, db: Database, date: str = "", submitted: str = ""):
        report_date = parse_report_date(date) if date else clock.today()
        return templates.TemplateResponse(
            request, "report.html", report_context(request, db, report_date, just_submitted=bool(submitted)))

    @app.post("/report/save", response_class=HTMLResponse)
    async def autosave_report(request: Request, db: Database, date: str = ""):
        """入力のたびに下書きとして保存し、合計時間などの集計欄だけを返す(入力欄は差し替えない)。"""
        report_date = parse_report_date(date)
        invalid_titles = await save_report_form(request, db, report_date)
        return templates.TemplateResponse(request, "_report_summary.html", report_context(
            request, db, report_date, invalid_titles=invalid_titles))

    @app.post("/report/submit")
    async def submit_report(request: Request, db: Database, date: str = ""):
        report_date = parse_report_date(date)
        invalid_titles = await save_report_form(request, db, report_date)
        if invalid_titles:
            return templates.TemplateResponse(request, "report.html", report_context(
                request, db, report_date, invalid_titles=invalid_titles), status_code=422)
        reports.submit(db, user_id, report_date)
        return RedirectResponse(f"/report?date={report_date.isoformat()}&submitted=1", status_code=303)

    @app.post("/report/items/{task_id}/remove", response_class=HTMLResponse)
    def remove_report_item(task_id: int, request: Request, db: Database, date: str = ""):
        report_date = parse_report_date(date)
        reports.remove_item(db, user_id, task_id, report_date)
        return templates.TemplateResponse(request, "_report_content.html", report_context(request, db, report_date))

    @app.post("/report/toggle-time-tracking", response_class=HTMLResponse)
    async def toggle_report_time_tracking(request: Request, db: Database, date: str = ""):
        """書きかけを保存してからON/OFFする。時刻の誤りがあれば入力欄を残して知らせる。"""
        report_date = parse_report_date(date)
        invalid_titles = await save_report_form(request, db, report_date)
        if invalid_titles:
            return templates.TemplateResponse(request, "_report_summary.html", report_context(
                request, db, report_date, invalid_titles=invalid_titles),
                headers={"HX-Retarget": "#report-summary", "HX-Reswap": "outerHTML"})
        reports.toggle_time_tracking(db, user_id, report_date)
        return templates.TemplateResponse(request, "_report_content.html", report_context(request, db, report_date))

    @app.post("/report/toggle-details", response_class=HTMLResponse)
    async def toggle_report_details(request: Request, db: Database, date: str = ""):
        """書きかけを保存してからON/OFFする。時刻の誤りがあれば入力欄を残して知らせる。"""
        report_date = parse_report_date(date)
        invalid_titles = await save_report_form(request, db, report_date)
        if invalid_titles:
            return templates.TemplateResponse(request, "_report_summary.html", report_context(
                request, db, report_date, invalid_titles=invalid_titles),
                headers={"HX-Retarget": "#report-summary", "HX-Reswap": "outerHTML"})
        reports.toggle_details(db, user_id, report_date)
        return templates.TemplateResponse(request, "_report_content.html", report_context(request, db, report_date))

    

    # ---- タスク操作(htmx。操作後はタスク画面の本体を再描画する) ----

    def flag_trouble_in_memo(db: sqlite3.Connection, task_id: int, title: str, memo: str) -> dict | None:
        """メモに困りごとの表現があれば困りごとフラグを付け、画面に出すお知らせを返す。"""
        if not (memo and blockers.contains_trouble_keyword(memo)):
            return None
        if blockers.raise_blocker(db, task_id, source="auto", note="メモの表現から自動検知") is None:
            return None
        return {"kind": "warn", "text": f"「{title}」のメモから困っている様子を検知し、相談中の付箋を付けました。違う場合は「解決した」で外せます。"}

    @app.post("/tasks", response_class=HTMLResponse)
    def quick_add_task(
        request: Request, db: Database, state: ViewState,
        title: Annotated[str, Form()] = "",
        destination: Annotated[str, Form()] = "today",
        importance: Annotated[str, Form()] = "",
        urgency: Annotated[str, Form()] = "",
        due_date: Annotated[str, Form()] = "",
        start_date: Annotated[str, Form()] = "",
        memo: Annotated[str, Form()] = "",
        parent_id: Annotated[str, Form()] = "",
    ):
        """入力欄からタスクを追加する。追加先を指定しなければ「今日やる」に入れる。"""
        task_title, tag_names = tasks.split_title_and_tags(title)
        if not task_title:
            return render_board(request, db, state)
        selected_parent_id = None
        if parent_id:
            try:
                selected_parent_id = int(parent_id)
            except ValueError:
                return render_board(request, db, state, {"kind": "warn", "text": "親タスクの選択を確認してください。"})
            candidate_ids = {parent["id"] for parent in tasks.list_parent_candidates(db, user_id)}
            if selected_parent_id not in candidate_ids:
                return render_board(request, db, state, {"kind": "warn", "text": "選んだ親タスクは追加先にできません。画面を読み直してください。"})
        today_iso = clock.today().isoformat()
        selected_due_date = due_date or (None if selected_parent_id is not None else today_iso)
        default_start_date = today_iso
        normalized_due_date = tasks.normalize_due_date(selected_due_date)
        if normalized_due_date and normalized_due_date < today_iso:
            default_start_date = normalized_due_date
        selected_start_date = start_date or (
            default_start_date if selected_parent_id is None else None
        )
        try:
            normalized_start_date = tasks.validate_start_date(selected_start_date, selected_due_date)
        except tasks.TaskDateError as error:
            return render_board(request, db, state, {"kind": "warn", "text": str(error)})
        task_id = tasks.create_task(
            db, user_id, task_title[:200], parent_id=selected_parent_id, tag_names=tag_names,
            importance=tasks.clamp_level(importance), urgency=tasks.clamp_level(urgency),
            due_date=selected_due_date, start_date=normalized_start_date,
            memo=memo.strip()[:tasks.MAX_MEMO_LENGTH],
        )
        if selected_parent_id is not None:
            today_candidate_ids = {task["id"] for task in daily.list_candidates(db, user_id)}
            if selected_parent_id in today_candidate_ids:
                daily.add_to_plan(db, task_id, clock.today())
        elif destination == "later":
            daily.move_to_later(db, task_id)
        else:
            daily.add_to_plan(db, task_id, clock.today())
        notice = flag_trouble_in_memo(db, task_id, task_title, memo.strip())
        if notice is None and destination == "later":
            # 明日以降にやるの段は閉じていることが多いので、追加できたことを知らせる
            notice = {"kind": "info", "text": f"「{task_title}」を明日以降にやるに追加しました。"}
        elif notice is None and state["tag"] and state["tag"] not in tag_names:
            notice = {"kind": "info", "text": f"「{task_title}」を追加しました(#{state['tag']} の絞り込みには入らないため、ここには表示されません)。"}
        return render_board(request, db, state, notice, highlight={"added": task_id})

    @app.post("/tasks/{task_id}/subtasks", response_class=HTMLResponse)
    def add_subtask(
        task_id: int, request: Request, db: Database, state: ViewState, parent: OwnedTask,
        title: Annotated[str, Form()] = "",
    ):
        subtask_title, tag_names = tasks.split_title_and_tags(title)
        highlight = None
        if parent and parent["parent_id"] is None and subtask_title:  # サブタスクは1階層のみ
            new_id = tasks.create_task(db, user_id, subtask_title[:200], parent_id=task_id, tag_names=tag_names)
            # 親が「今日やる」にあれば、子も今日やるに入れて親の下に並べる
            if task_id in {task["id"] for task in daily.list_candidates(db, user_id)}:
                daily.add_to_plan(db, new_id, clock.today())
            highlight = {"added": new_id}
        return render_board(request, db, state, highlight=highlight)

    @app.post("/tasks/{task_id}/status/{status}", response_class=HTMLResponse)
    def change_status(task_id: int, status: str, request: Request, db: Database, state: ViewState, task: OwnedTask):
        """状態を変更する。親の完了は進捗欄のボタンを押したときだけ行う。"""
        highlight, notice = None, None
        if task is None:
            notice = {"kind": "warn", "text": "操作できるタスクが見つかりません。画面を読み直してください。"}
        elif status not in tasks.STATUSES:
            notice = {"kind": "warn", "text": "指定された状態には変更できません。"}
        else:
            try:
                tasks.set_status(db, task_id, status)
                if status == "done" and task["status"] != "done":
                    highlight = {"completed": task_id}
                    parent = tasks.get_owned_task(db, user_id, task["parent_id"]) if task["parent_id"] is not None else None
                    if parent is not None and parent["status"] == "done":
                        highlight["completed_parent"] = parent["id"]
            except (tasks.UnfinishedSubtasksError, tasks.ParentTaskStatusError, tasks.TaskRelationshipError) as error:
                db.rollback()
                notice = {"kind": "warn", "text": str(error)}
        response = render_board(request, db, state, notice, highlight=highlight)
        if state["view"] == "archive" and notice is None:
            response.headers["HX-Redirect"] = "/"
        return response

    @app.post("/tasks/{task_id}/progress/{progress}", response_class=HTMLResponse)
    def old_change_progress(task_id: int, progress: int, request: Request, db: Database, state: ViewState, task: OwnedTask):
        """古い画面の操作で状態を変更させず、新しい操作への案内を返す。"""
        return render_board(request, db, state, {
            "kind": "warn", "text": "％での進捗操作は終了しました。「開始する」「済」で状態を変更してください。",
        })

    @app.post("/tasks/{task_id}/blocker", response_class=HTMLResponse)
    def toggle_task_blocker(task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask):
        """「相談したい」で困りごとを立て、理由を書く付箋を開く。「解決した」で外す。"""
        highlight = None
        if task and blockers.toggle_blocker(db, task_id):
            highlight = {"flagged": task_id}
        return render_board(request, db, state, highlight=highlight)

    @app.post("/tasks/{task_id}/blocker/note", response_class=HTMLResponse)
    def save_blocker_note(
        task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask,
        note: Annotated[str, Form()] = "",
    ):
        if task:
            blockers.update_open_blocker_note(db, task_id, note)
        return render_board(request, db, state)

    @app.post("/tasks/{task_id}/waiting", response_class=HTMLResponse)
    def toggle_task_waiting(task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask):
        """「返事待ち」で待ちにし、待っている相手を書く欄を開く。「再開する」で解除する。"""
        highlight = {"waiting": task_id} if task and tasks.toggle_waiting(db, task_id) else None
        return render_board(request, db, state, highlight=highlight)

    @app.post("/tasks/{task_id}/waiting/note", response_class=HTMLResponse)
    def save_waiting_note(
        task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask,
        note: Annotated[str, Form()] = "",
    ):
        if task:
            tasks.set_waiting_note(db, task_id, note)
        return render_board(request, db, state)

    @app.post("/tasks/{task_id}/today", response_class=HTMLResponse)
    def toggle_today(task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask):
        """「今日やる」と「明日以降にやる」の間で移す。移した先が閉じていても見失わないよう、元に戻すボタン付きで知らせる。"""
        notice = None
        if task:
            was_today = daily.is_in_today(db, user_id, task_id)
            daily.toggle_today_plan(db, user_id, task_id)
            destination = "明日以降にやる" if was_today else "今日やる"
            notice = {
                "kind": "info", "text": f"「{task['title']}」を{destination}ことにしました。",
                "undo_url": f"/tasks/{task_id}/today?{build_view_query(state)}",
            }
        return render_board(request, db, state, notice)

    @app.post("/tasks/{task_id}/report", response_class=HTMLResponse)
    def toggle_report_item(task_id: int, request: Request, db: Database, state: ViewState):
        """今日の日報に入れる/外す。選んだ内容はすぐ保存するので、画面を移っても消えない。"""
        reports.toggle_item(db, user_id, task_id, clock.today())
        return render_board(request, db, state)

    @app.post("/tasks/{task_id}/delete", response_class=HTMLResponse)
    def delete_task(task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask):
        notice = None
        if task:
            try:
                # 日報に入っていたタスクなら、その日報を「修正あり」にする(提出済みの日報が黙って変わらないように)
                reports.mark_reports_changed_for_task(db, task_id)
                tasks.delete_task(db, task_id)
            except tasks.TaskRelationshipError as error:
                db.rollback()
                notice = {"kind": "warn", "text": str(error)}
        return render_board(request, db, state, notice)

    @app.post("/tasks/{task_id}/edit", response_class=HTMLResponse)
    async def edit_task(
        task_id: int, request: Request, db: Database, state: ViewState, task: OwnedTask,
        title: Annotated[str, Form()] = "",
        due_date: Annotated[str, Form()] = "",
        start_date: Annotated[str, Form()] = "",
        memo: Annotated[str, Form()] = "",
        tag_text: Annotated[str, Form()] = "",
        recurrence_rule: Annotated[str | None, Form()] = None,
        importance: Annotated[str, Form()] = "",
        urgency: Annotated[str, Form()] = "",
        parent_id: Annotated[str | None, Form()] = None,
        confirm_clear_recurrence: Annotated[bool, Form()] = False,
    ):
        if task is None:
            return render_board(request, db, state, {"kind": "warn", "text": "操作できるタスクが見つかりません。"})
        # Formの既定値では空文字もNoneになるため、「解除」と「項目なし」を区別する。
        submitted_form = await request.form()
        if "parent_id" in submitted_form:
            parent_id = str(submitted_form["parent_id"])
        if "recurrence_rule" in submitted_form:
            recurrence_rule = str(submitted_form["recurrence_rule"])
        new_title = title.strip() or task["title"]
        new_recurrence_rule = recurrence_rule if recurrence_rule is not None else task["recurrence_rule"]
        new_importance = tasks.clamp_level(importance, default=task["importance"])
        new_urgency = tasks.clamp_level(urgency, default=task["urgency"])
        draft = {
            "task_id": task_id, "title": new_title, "due_date": due_date, "start_date": start_date, "memo": memo,
            "tag_text": tag_text, "recurrence_rule": new_recurrence_rule,
            "importance": new_importance, "urgency": new_urgency,
            "parent_id": parent_id if parent_id is not None else str(task["parent_id"] or ""),
        }
        old_section = daily.task_board_section(db, user_id, task_id)
        relationship_changed = False
        try:
            normalized_start_date = tasks.validate_start_date(start_date, due_date)
            tasks.update_task(
                db, task_id, title=new_title[:200], due_date=tasks.normalize_due_date(due_date),
                start_date=normalized_start_date,
                memo=memo.strip()[:tasks.MAX_MEMO_LENGTH], importance=new_importance, urgency=new_urgency,
            )
            tasks.set_tags(db, task_id, tasks.parse_tag_names(tag_text))
            if parent_id is not None:
                if parent_id and (not parent_id.isascii() or not parent_id.isdecimal() or len(parent_id) > 19):
                    raise tasks.TaskRelationshipError("親タスクを候補から選び直してください。")
                relationship_changed = tasks.change_parent(
                    db, user_id, task_id, int(parent_id) if parent_id else None,
                    confirm_clear_recurrence=confirm_clear_recurrence,
                )
            tasks.set_recurrence_rule(
                db, user_id, task_id, new_recurrence_rule or None,
                confirm_clear_recurrence=confirm_clear_recurrence,
            )
        except (tasks.TaskRelationshipError, tasks.TaskDateError) as error:
            db.rollback()
            return render_board(
                request, db, state, {"kind": "warn", "text": str(error)}, highlight={"edit_draft": draft},
            )

        notice = None
        if relationship_changed:
            new_section = daily.task_board_section(db, user_id, task_id)
            destination = (
                f"「{new_title}」を親から外して独立させました。" if not parent_id
                else f"「{new_title}」を親タスクに結び付けました。親子は同じ段に並びます。"
            )
            location = f"表示先は「{new_section}」です。" if old_section == new_section else f"「{old_section}」から「{new_section}」へ移りました。"
            notice = {"kind": "info", "text": destination + location}
        if memo.strip() != task["memo"]:
            trouble_notice = flag_trouble_in_memo(db, task_id, new_title, memo.strip())
            if trouble_notice:
                notice = {"kind": "warn", "text": (notice["text"] + " " if notice else "") + trouble_notice["text"]}
        return render_board(request, db, state, notice)

    # ---- Copilot(MCP経由)からの更新の取り消し(連携は一時停止中。ルートのみ残す) ----

    @app.post("/ai/reports/{report_id}/undo", response_class=HTMLResponse)
    def undo_ai_report(report_id: int, request: Request, db: Database, state: ViewState):
        undone = ai_actions.undo_report(db, user_id, report_id)
        notice = {"kind": "ok", "text": "Copilot からの更新を取り消しました。"} if undone else None
        return render_board(request, db, state, notice)

    return app


app = create_app()
