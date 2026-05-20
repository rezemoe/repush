import curses
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import requests


def get_git_token():
    return os.getenv("GH_TOKEN") or ""


class FetchError(Exception):
    pass


class RepushAPI:
    API = "https://api.github.com"

    def __init__(self, gh_token, log_callback=None):
        self.gh_token = gh_token
        self.log = log_callback or (lambda x: None)
        self.session = requests.session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Repush/1.0",
                "Authorization": f"token {self.gh_token}",
            }
        )
        self.rate_reset = None

    def _api(self, path: str, params: dict = None) -> requests.Response:
        url = f"{self.API}{path}"
        while True:
            try:
                r = self.session.get(url, params=params, timeout=30)
            except Exception as exc:
                raise FetchError(f"Request failed: {exc}")

            rst = r.headers.get("X-RateLimit-Reset")
            if rst is not None:
                self.rate_reset = int(rst)

            if r.status_code == 403 and "rate limit" in r.text.lower():
                wait = (
                    max(
                        0, (self.rate_reset or int(time.time()) + 60) - int(time.time())
                    )
                    + 2
                )
                self.log(f"Rate-limited! Waiting {wait}s")
                time.sleep(wait)
                continue
            return r

    def get_user_repos(self):
        repos = []
        page = 1
        while True:
            r = self._api(
                "/user/repos",
                params={"affiliation": "owner", "per_page": 100, "page": page},
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            repos.extend(data)
            page += 1
        return repos

    def get_user_orgs(self):
        r = self._api("/user/orgs")
        return r.json() if r.status_code == 200 else []

    def get_org_repos(self, org_name):
        repos = []
        page = 1
        while True:
            r = self._api(
                f"/orgs/{org_name}/repos", params={"per_page": 100, "page": page}
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            repos.extend(data)
            page += 1
        return repos


class CursesApp:
    def __init__(self, stdscr, api):
        self.stdscr = stdscr
        self.api = api
        self.api.log = self.log_msg
        curses.curs_set(0)
        self.stdscr.nodelay(1)
        curses.start_color()
        curses.use_default_colors()

        # Color pairs
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_GREEN)  # Highlight

        self.state = "MAIN_MENU"
        self.menu_idx = 0
        self.clone_method = "ssh"

        self.list_items = []
        self.list_title = ""
        self.scroll_offset = 0

        self.input_text = ""

        self.message = ""
        self.is_running = True

        self.bg_thread = None

        self.repo_name = ""
        self.progress_total = 0
        self.progress_curr = 0
        self.progress_desc = ""
        self.logs = []
        self.cancel_event = threading.Event()

    def log_msg(self, msg):
        # Strip simple internal markup if any somehow slips through
        import re

        clean_msg = re.sub(r"\[/?(?:cyan|red|yellow|green|bold|blue)\]", "", msg)
        self.logs.append(clean_msg)
        if len(self.logs) > 100:
            self.logs.pop(0)

    def draw_box(self, y, x, h, w, title=""):
        try:
            self.stdscr.addch(y, x, curses.ACS_ULCORNER)
            self.stdscr.addch(y, x + w - 1, curses.ACS_URCORNER)
            self.stdscr.addch(y + h - 1, x, curses.ACS_LLCORNER)
            self.stdscr.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
            self.stdscr.hline(y, x + 1, curses.ACS_HLINE, w - 2)
            self.stdscr.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
            self.stdscr.vline(y + 1, x, curses.ACS_VLINE, h - 2)
            self.stdscr.vline(y + 1, x + w - 1, curses.ACS_VLINE, h - 2)
            if title:
                self.stdscr.addstr(
                    y, x + 2, f" {title} ", curses.color_pair(2) | curses.A_BOLD
                )
        except curses.error:
            pass

    def render(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        if h < 10 or w < 30:
            try:
                self.stdscr.addstr(0, 0, "Terminal too small")
            except curses.error:
                pass
            self.stdscr.noutrefresh()
            curses.doupdate()
            return

        title = " Repush - Git History Anonymizer "
        try:
            self.stdscr.addstr(
                0,
                max(0, w // 2 - len(title) // 2),
                title,
                curses.color_pair(2) | curses.A_BOLD,
            )
        except curses.error:
            pass

        if self.state == "MAIN_MENU":
            self.draw_box(2, 2, h - 4, w - 4, "Main Menu")
            items = [
                f"Clone Method: [{'SSH (Preferred)' if self.clone_method == 'ssh' else 'HTTPS'}]",
                "1. All my repositories",
                "2. Repositories in an organization",
                "3. Manual input",
                "Quit",
            ]
            for i, item in enumerate(items):
                attr = curses.color_pair(5) if i == self.menu_idx else curses.A_NORMAL
                try:
                    self.stdscr.addstr(4 + i, 4, item, attr)
                except curses.error:
                    pass

            footer = "Use j/k or UP/DOWN to navigate, ENTER to select, q to quit"
            try:
                self.stdscr.addstr(
                    h - 1,
                    max(0, w // 2 - len(footer) // 2),
                    footer,
                    curses.color_pair(4),
                )
            except curses.error:
                pass

        elif self.state in ("LIST_REPOS", "LIST_ORGS"):
            self.draw_box(2, 2, h - 4, w - 4, self.list_title)
            if self.message:
                try:
                    self.stdscr.addstr(4, 4, self.message, curses.color_pair(4))
                except curses.error:
                    pass
            else:
                max_items = h - 8
                if self.menu_idx < self.scroll_offset:
                    self.scroll_offset = self.menu_idx
                elif self.menu_idx >= self.scroll_offset + max_items:
                    self.scroll_offset = self.menu_idx - max_items + 1

                for i in range(max_items):
                    idx = self.scroll_offset + i
                    if idx < len(self.list_items):
                        item = self.list_items[idx]
                        attr = (
                            curses.color_pair(5)
                            if idx == self.menu_idx
                            else curses.A_NORMAL
                        )
                        if len(item) > w - 8:
                            item = item[: w - 11] + "..."
                        try:
                            self.stdscr.addstr(4 + i, 4, item, attr)
                        except curses.error:
                            pass

            footer = "Use j/k or UP/DOWN to navigate, ENTER to select, ESC/q to go back"
            try:
                self.stdscr.addstr(
                    h - 1,
                    max(0, w // 2 - len(footer) // 2),
                    footer,
                    curses.color_pair(4),
                )
            except curses.error:
                pass

        elif self.state == "MANUAL_INPUT":
            self.draw_box(2, 2, 5, w - 4, "Manual Input")
            try:
                self.stdscr.addstr(3, 4, "Enter repository (owner/repo):")
                self.stdscr.addstr(4, 4, self.input_text + "_", curses.color_pair(1))
            except curses.error:
                pass

            footer = "Type repo name, ENTER to submit, ESC to cancel"
            try:
                self.stdscr.addstr(
                    h - 1,
                    max(0, w // 2 - len(footer) // 2),
                    footer,
                    curses.color_pair(4),
                )
            except curses.error:
                pass

        elif self.state == "FETCHING":
            self.draw_box(2, 2, h - 4, w - 4, "Working")
            try:
                self.stdscr.addstr(
                    h // 2,
                    max(0, w // 2 - len(self.message) // 2),
                    self.message,
                    curses.color_pair(2),
                )
            except curses.error:
                pass

        elif self.state == "PROCESS":
            self.draw_box(2, 2, 6, w - 4, f"Processing: {self.repo_name}")
            try:
                self.stdscr.addstr(3, 4, self.progress_desc)

                bar_w = w - 10
                if self.progress_total > 0:
                    pct = self.progress_curr / self.progress_total
                else:
                    pct = 0
                filled = int(bar_w * pct)
                bar = "[" + "=" * filled + " " * (bar_w - filled) + "]"
                self.stdscr.addstr(4, 4, bar, curses.color_pair(1))
                self.stdscr.addstr(
                    5,
                    4,
                    f"{self.progress_curr}/{self.progress_total} commits processed",
                )
            except curses.error:
                pass

            log_h = h - 10
            self.draw_box(9, 2, log_h, w - 4, "Logs")

            max_logs = log_h - 2
            display_logs = (
                self.logs[-max_logs:] if len(self.logs) > max_logs else self.logs
            )
            for i, l in enumerate(display_logs):
                if len(l) > w - 8:
                    l = l[: w - 11] + "..."
                try:
                    self.stdscr.addstr(10 + i, 4, l)
                except curses.error:
                    pass

            footer = "Press 'c' to Cancel, 'q' or ESC to return (when done)"
            try:
                self.stdscr.addstr(
                    h - 1,
                    max(0, w // 2 - len(footer) // 2),
                    footer,
                    curses.color_pair(4),
                )
            except curses.error:
                pass

        self.stdscr.noutrefresh()
        curses.doupdate()

    def run(self):
        while self.is_running:
            self.render()
            try:
                c = self.stdscr.getch()
            except curses.error:
                c = -1

            if c != -1:
                self.handle_input(c)

            time.sleep(0.01)

    def handle_input(self, c):
        if self.state == "MAIN_MENU":
            if c in (ord("q"), 27):
                self.is_running = False
            elif c in (curses.KEY_DOWN, ord("j")):
                self.menu_idx = min(4, self.menu_idx + 1)
            elif c in (curses.KEY_UP, ord("k")):
                self.menu_idx = max(0, self.menu_idx - 1)
            elif c in (10, 13, curses.KEY_ENTER):
                if self.menu_idx == 0:
                    self.clone_method = "https" if self.clone_method == "ssh" else "ssh"
                elif self.menu_idx == 1:
                    self.start_fetch("my_repos")
                elif self.menu_idx == 2:
                    self.start_fetch("orgs")
                elif self.menu_idx == 3:
                    self.state = "MANUAL_INPUT"
                    self.input_text = ""
                elif self.menu_idx == 4:
                    self.is_running = False

        elif self.state in ("LIST_REPOS", "LIST_ORGS"):
            if c in (ord("q"), 27):
                self.state = "MAIN_MENU"
                self.menu_idx = 0
            elif c in (curses.KEY_DOWN, ord("j")):
                if self.list_items:
                    self.menu_idx = min(len(self.list_items) - 1, self.menu_idx + 1)
            elif c in (curses.KEY_UP, ord("k")):
                self.menu_idx = max(0, self.menu_idx - 1)
            elif c in (10, 13, curses.KEY_ENTER):
                if self.list_items:
                    selected = self.list_items[self.menu_idx]
                    if self.state == "LIST_ORGS":
                        self.start_fetch("org_repos", selected)
                    else:
                        self.start_process(selected)

        elif self.state == "MANUAL_INPUT":
            if c == 27:
                self.state = "MAIN_MENU"
                self.menu_idx = 0
            elif c in (10, 13, curses.KEY_ENTER):
                if self.input_text.strip():
                    self.start_process(self.input_text.strip())
            elif c in (curses.KEY_BACKSPACE, 127, 8):
                self.input_text = self.input_text[:-1]
            elif 32 <= c <= 126:
                self.input_text += chr(c)

        elif self.state == "PROCESS":
            if c == ord("c"):
                self.cancel_event.set()
                self.log_msg("Cancel requested...")
            elif c in (ord("q"), 27):
                if not self.bg_thread or not self.bg_thread.is_alive():
                    self.state = "MAIN_MENU"
                    self.menu_idx = 0

    def start_fetch(self, fetch_type, arg=None):
        self.state = "FETCHING"
        self.message = "Fetching data from GitHub API..."
        self.bg_thread = threading.Thread(
            target=self._fetch_task, args=(fetch_type, arg), daemon=True
        )
        self.bg_thread.start()

    def _fetch_task(self, fetch_type, arg):
        try:
            if fetch_type == "my_repos":
                repos = self.api.get_user_repos()
                self.list_items = [r["full_name"] for r in repos]
                self.list_title = "Your Repositories"
                self.state = "LIST_REPOS"
            elif fetch_type == "orgs":
                orgs = self.api.get_user_orgs()
                self.list_items = [o["login"] for o in orgs]
                self.list_title = "Your Organizations"
                self.state = "LIST_ORGS"
            elif fetch_type == "org_repos":
                repos = self.api.get_org_repos(arg)
                self.list_items = [r["full_name"] for r in repos]
                self.list_title = f"Repositories in {arg}"
                self.state = "LIST_REPOS"

            self.menu_idx = 0
            if not self.list_items:
                self.message = "No items found. Press ESC to return."
            else:
                self.message = ""
        except Exception as e:
            self.state = "LIST_REPOS"
            self.list_items = []
            self.message = f"Error fetching data: {e} - Press ESC to return"

    def start_process(self, repo_name):
        self.repo_name = repo_name
        self.state = "PROCESS"
        self.logs = []
        self.cancel_event.clear()
        self.progress_total = 0
        self.progress_curr = 0
        self.progress_desc = "Initializing..."
        self.bg_thread = threading.Thread(target=self._process_task, daemon=True)
        self.bg_thread.start()

    def _process_task(self):
        gh_token = self.api.gh_token
        self.log_msg(f"Verifying repository {self.repo_name}...")
        try:
            r = self.api._api(f"/repos/{self.repo_name}")
            if r.status_code != 200:
                self.log_msg(
                    f"Repository not found or inaccessible (HTTP {r.status_code})"
                )
                self.progress_desc = "Failed"
                return
        except Exception as e:
            self.log_msg(f"Error verifying repo: {e}")
            self.progress_desc = "Failed"
            return

        temp_dir = os.path.join(tempfile.gettempdir(), "repush_workspace")
        path = os.path.abspath(os.path.join(temp_dir, self.repo_name.replace("/", "_")))
        os.makedirs(temp_dir, exist_ok=True)

        try:
            if self.clone_method == "ssh":
                url = f"git@github.com:{self.repo_name}.git"
                if not os.path.exists(path):
                    self.log_msg(f"Cloning {self.repo_name} via SSH...")
                    subprocess.run(
                        ["git", "clone", "--bare", url, path],
                        check=True,
                        capture_output=True,
                    )
                else:
                    self.log_msg(f"Fetching latest changes via SSH...")
                    subprocess.run(
                        ["git", "--git-dir", path, "fetch", "--all"],
                        check=True,
                        capture_output=True,
                    )
            else:
                url = f"https://github.com/{self.repo_name}.git"
                auth_header = f"AUTHORIZATION: bearer {gh_token}"
                if not os.path.exists(path):
                    self.log_msg(f"Cloning {self.repo_name} via HTTPS...")
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            f"http.extraHeader={auth_header}",
                            "clone",
                            "--bare",
                            url,
                            path,
                        ],
                        check=True,
                        capture_output=True,
                    )
                else:
                    self.log_msg(f"Fetching latest changes via HTTPS...")
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            f"http.extraHeader={auth_header}",
                            "--git-dir",
                            path,
                            "fetch",
                            "--all",
                        ],
                        check=True,
                        capture_output=True,
                    )
        except subprocess.CalledProcessError as e:
            self.log_msg(f"Git error: {e.stderr.decode('utf-8', errors='ignore')}")
            self.progress_desc = "Git Error"
            return

        os.makedirs("cache", exist_ok=True)
        cache_file = os.path.join(
            "cache", f"{self.repo_name.replace('/', '_')}_cache.json"
        )
        cache = {}
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                cache = json.load(f)

        try:
            cmd = [
                "git",
                "--git-dir",
                path,
                "rev-list",
                "--all",
                "--reverse",
                "--topo-order",
            ]
            commits = subprocess.check_output(cmd).decode("utf-8").splitlines()
        except subprocess.CalledProcessError:
            self.log_msg(f"Failed to get commits")
            self.progress_desc = "Failed"
            return

        if not commits:
            self.log_msg("No commits found.")
            self.progress_desc = "Empty Repo"
            return

        self.progress_total = len(commits)
        self.progress_curr = 0
        self.progress_desc = "Redacting commits..."

        try:
            for i, commit in enumerate(commits):
                if self.cancel_event.is_set():
                    self.log_msg("Cancelled by user. Saving cache...")
                    break

                if commit in cache:
                    self.progress_curr += 1
                    continue

                cmd = [
                    "git",
                    "--git-dir",
                    path,
                    "log",
                    "-1",
                    "--format=%T%n%P%n%ad%n%cd",
                    "--date=raw",
                    commit,
                ]
                info = subprocess.check_output(cmd).decode("utf-8").strip().split("\n")
                tree = info[0]
                parents = info[1].split() if len(info) > 1 and info[1] else []
                adate = info[2] if len(info) > 2 else ""
                cdate = info[3] if len(info) > 3 else ""

                new_parents = [cache.get(p, p) for p in parents]

                env = os.environ.copy()
                env["GIT_AUTHOR_NAME"] = "Marisa"
                env["GIT_AUTHOR_EMAIL"] = "repush@mishashto.com"
                env["GIT_COMMITTER_NAME"] = "Marisa"
                env["GIT_COMMITTER_EMAIL"] = "repush@mishashto.com"
                if adate:
                    parts = adate.split()
                    if len(parts) == 2:
                        adate = f"{parts[0]} +0000"
                    env["GIT_AUTHOR_DATE"] = adate
                if cdate:
                    parts = cdate.split()
                    if len(parts) == 2:
                        cdate = f"{parts[0]} +0000"
                    env["GIT_COMMITTER_DATE"] = cdate

                orig_msg_cmd = [
                    "git",
                    "--git-dir",
                    path,
                    "log",
                    "-1",
                    "--format=%B",
                    commit,
                ]
                orig_msg = (
                    subprocess.check_output(orig_msg_cmd)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                import re

                orig_msg = re.sub(
                    r"(?im)^(?:Co-authored-by|Signed-off-by|Reviewed-by|Acked-by):.*$\n?",
                    "",
                    orig_msg,
                ).strip()
                new_msg = f"{orig_msg}\n\nThis commit has been anonymized with Repush."

                args = [
                    "git",
                    "--git-dir",
                    path,
                    "commit-tree",
                    tree,
                    "-m",
                    new_msg,
                ]
                for p in new_parents:
                    args.extend(["-p", p])

                new_sha = subprocess.check_output(args, env=env).decode("utf-8").strip()
                cache[commit] = new_sha

                if i % 10 == 0:
                    self.log_msg(f"Redacted {commit[:8]} -> {new_sha[:8]}")
                self.progress_curr += 1

        finally:
            with open(cache_file, "w") as f:
                json.dump(cache, f)

        if not self.cancel_event.is_set():
            self.log_msg("Updating repository references...")
            refs_out = subprocess.check_output(
                [
                    "git",
                    "--git-dir",
                    path,
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                ]
            )
            refs = [line.split() for line in refs_out.decode("utf-8").splitlines()]
            for refname, old_sha in refs:
                if old_sha in cache:
                    subprocess.run(
                        [
                            "git",
                            "--git-dir",
                            path,
                            "update-ref",
                            refname,
                            cache[old_sha],
                        ]
                    )
                else:
                    try:
                        peeled = (
                            subprocess.check_output(
                                [
                                    "git",
                                    "--git-dir",
                                    path,
                                    "rev-parse",
                                    "--verify",
                                    "--quiet",
                                    f"{refname}^{{commit}}",
                                ]
                            )
                            .decode("utf-8")
                            .strip()
                        )
                        if peeled and peeled in cache:
                            subprocess.run(
                                [
                                    "git",
                                    "--git-dir",
                                    path,
                                    "update-ref",
                                    refname,
                                    cache[peeled],
                                ]
                            )
                    except subprocess.CalledProcessError:
                        pass

            self.log_msg("Pruning unreferenced objects to enforce anonymity...")
            subprocess.run(
                ["git", "--git-dir", path, "reflog", "expire", "--expire=now", "--all"],
                capture_output=True,
            )
            subprocess.run(
                ["git", "--git-dir", path, "gc", "--prune=now", "--aggressive"],
                capture_output=True,
            )

            self.log_msg(f"Pushing redacted history to remote...")
            try:
                if self.clone_method == "ssh":
                    url = f"git@github.com:{self.repo_name}.git"
                    subprocess.run(
                        ["git", "--git-dir", path, "push", "--force", url, "--all"],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "--git-dir", path, "push", "--force", url, "--tags"],
                        check=True,
                        capture_output=True,
                    )
                else:
                    url = f"https://github.com/{self.repo_name}.git"
                    auth_header = f"AUTHORIZATION: bearer {gh_token}"
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            f"http.extraHeader={auth_header}",
                            "--git-dir",
                            path,
                            "push",
                            "--force",
                            url,
                            "--all",
                        ],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            f"http.extraHeader={auth_header}",
                            "--git-dir",
                            path,
                            "push",
                            "--force",
                            url,
                            "--tags",
                        ],
                        check=True,
                        capture_output=True,
                    )
                self.log_msg(f"Done! History pushed to remote for '{self.repo_name}'.")
                self.log_msg("Note: GitHub caches the Contributors graph heavily.")
                self.log_msg("It may take ~24h for old authors to disappear visually.")
                self.log_msg(
                    "Also, commits in read-only Pull Requests cannot be rewritten."
                )
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e)
                self.log_msg(f"Failed to push: {err}")

            self.log_msg("Cleaning up local repository from disk...")
            import shutil

            shutil.rmtree(path, ignore_errors=True)

            self.progress_desc = "Complete"
        else:
            self.progress_desc = "Cancelled"


def main(stdscr):
    gh_token = get_git_token()
    if not gh_token:
        curses.endwin()
        print("GH_TOKEN environment variable is not set. Please set it to proceed.")
        sys.exit(1)

    api = RepushAPI(gh_token)
    app = CursesApp(stdscr, api)
    app.run()


if __name__ == "__main__":
    if not get_git_token():
        print("GH_TOKEN environment variable is not set. Please set it to proceed.")
        sys.exit(1)
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
