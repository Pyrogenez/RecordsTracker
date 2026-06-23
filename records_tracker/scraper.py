"""Playwright-based scraper for the St. Pete Public Records Center.

Target portal: https://stpetefl.mycusthelp.com/WEBAPP/_rs/Login.aspx
  - ASP.NET WebForms + DevExpress ASPx controls + cookieless session path (S(...))
  - Session carried in URL path; the browser follows redirects, so we navigate
    normally and let cookies/session ride along.

Read-only policy: we only GET pages, click Details / pagination / "View …"
expanders, and trigger attachment downloads. No Save / New Message / Cancel /
form edits. This scraper is idempotent from the server's perspective.

Real DOM landmarks (mapped 2026-04-20 against the live portal):

  Login page  (Login.aspx)
    #ASPxFormLayout1_txtUsername_I    email field
    #ASPxFormLayout1_txtPassword_I    password field
    #ASPxFormLayout1_btnLogin_I       submit (DevExpress button with _BTC overlay)

  List page   (CustomerIssues.aspx)
    #roundPanel_issuesListView                                container
    roundPanel_issuesListView_ctrl{N}_referenceLnk            reference text (P######-######)
    roundPanel_issuesListView_ctrl{N}_primaryCustomer         requester name
    roundPanel_issuesListView_ctrl{N}_divHyperlink            progress stage text
    roundPanel_issuesListView_ctrl{N}_btnDetails_I            Details button
    #roundPanel_issuesListView_listDataPager                  pagination row
      - NO dedicated Next button. Anchors: « (First, ctl00$ctl00), numeric
        page links (ctl01$ctlNN, text = page number), … (jump, ctl01$ctlNN),
        » (Last, ctl02$ctl00). Current page is rendered as a plain <span>
        child of the pager with digit text. Advance one page by clicking the
        anchor whose visible text equals current_page + 1.

  Detail page (RequestEdit.aspx?rid={rid})
    #RequestEditFormLayout_roType            Request Type value
    #RequestEditFormLayout_roContactEmail    Requester Email value
    #RequestEditFormLayout_roReferenceNo     Reference No value
    #RequestEditFormLayout_roStatus          Status value
    #requestData_CustomFieldsFormLayout      "Additional Information" section
      - each row has a caption SPAN (class dxflCaption_Moderno) and a value SPAN
        with an id like requestData_CustomFieldsFormLayout_cf_##
    #btnViewMessage_I   "View Message(s)" — click to reveal message panel
    #btnViewFile_I      "View File(s)"    — click to reveal attachments panel
    #MessageThread                              message thread container
      - each message: table[id^="rptMessageHistory_ctl##__pnlMessages_{msgId}"]
      - header TD: td.dxrpHeader_Moderno → "On {MM/DD/YYYY H:MM:SS AM/PM}, {sender} wrote:"
      - body TD:   td.dxrp.dxrpcontent
    #dvAttachments                              attachments container
      - per file: <a id="rptAttachments_ctl##_lnkStream"
                     onclick="IsDownloadable('rptAttachments_ctl##_lnkStreamCloud',
                                             {attachmentId}, {rid}, …)"
                     href="javascript:__doPostBack('rptAttachments$ctl##$lnkStreamCloud','')">
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from dateutil import parser as dateparser
from playwright.sync_api import (
    BrowserContext,
    Download,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from . import human_delay
from .config import Config, Credentials

log = logging.getLogger(__name__)


# -- regex patterns for parsing ------------------------------------------------

REF_PATTERN = re.compile(r"P\d{6}-\d{6}")
PANEL_MSG_ID_PATTERN = re.compile(r"pnlMessages_(\d+)$")
MESSAGE_HEADER_PATTERN = re.compile(
    r"On\s+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s*,\s*(.+?)\s+wrote:",
    re.IGNORECASE,
)
ATTACHMENT_ONCLICK_PATTERN = re.compile(
    r'IsDownloadable\(\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)'
)

# Maps the "Additional Information" caption text to the field key we store.
# Caption text is stripped of trailing ":" and compared case-insensitively.
CUSTOM_FIELD_LABEL_MAP: dict[str, str] = {
    "category of requester":           "category",
    "department":                      "department",
    "type of record(s) requested":     "records_type",
    "describe the record(s) requested": "description",
    "preferred method to receive records": "preferred_method",
}


# -- data classes --------------------------------------------------------------

@dataclass
class RequestSummary:
    request_id: str
    rid: int
    status: str | None
    final_state: str | None
    detail_url: str


@dataclass
class MessageRecord:
    message_id: int
    sent_at: str
    sender: str
    subject: str | None
    body: str
    sequence_num: int
    is_auto_ack: bool


@dataclass
class AttachmentRecord:
    attachment_id: int
    filename: str
    postback_target: str  # e.g. "rptAttachments$ctl00$lnkStreamCloud"


class RequestNotFoundError(RuntimeError):
    """Raised when the portal redirects a detail-page navigation to its
    error.aspx with a message like 'Issue Not Found'. Callers can catch this
    to distinguish an unknown/inaccessible rid from a transient failure."""

    def __init__(self, request_id: str, rid: int, message: str):
        super().__init__(
            f"Portal reports '{message}' for {request_id} (rid={rid}) — "
            f"this rid is not recognized or not accessible to this account."
        )
        self.request_id = request_id
        self.rid = rid
        self.portal_message = message


# -- scraper -------------------------------------------------------------------

class PortalScraper:
    """Context-managed Playwright session against the Public Records Center."""

    def __init__(self, credentials: Credentials, config: Config, log_dir: Path):
        self.creds = credentials
        self.config = config
        self.log_dir = log_dir
        self._pw: Playwright | None = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # -- lifecycle --
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.config.headless)
        self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_timeout(self.config.page_load_timeout_seconds * 1000)
        self._page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    @property
    def page(self) -> Page:
        assert self._page is not None, "Scraper not started (use `with` block)"
        return self._page

    # -- auth ------------------------------------------------------------------
    def login(self) -> None:
        """Log in with stored credentials.

        Portal login flow: fill the two DevExpress text boxes (they render as
        a wrapped <input id="*_I"> inside a table), then press Enter in the
        password field. Enter triggers the form's onkeypress handler which
        invokes the same __doPostBack that clicking the button would — this
        sidesteps DevExpress' overlapping span/img overlays that block
        ordinary clicks.
        """
        log.info("Navigating to login page: %s", self.creds.portal_url)
        self.page.goto(self.creds.portal_url, wait_until="domcontentloaded")

        # If we land on the St. Pete transparency page rather than the portal
        # itself, follow the "Make a Records Request" link.
        if "mycusthelp.com" not in self.page.url and "govqa" not in self.page.url:
            bridge = self.page.locator('a:has-text("Make a Records Request")').first
            if bridge.count() > 0:
                with self.page.expect_navigation(wait_until="domcontentloaded"):
                    bridge.click()

        # Wait for the login form to render. It's inside a DevExpress form layout.
        try:
            self.page.wait_for_selector("#ASPxFormLayout1_txtUsername_I", timeout=15000)
        except PlaywrightTimeout as e:
            raise RuntimeError(
                f"Login form did not render at {self.page.url}. "
                "Open credentials.json and double-check `portal_url`, or run with "
                "headless=false in config.json to watch it."
            ) from e

        self.page.fill("#ASPxFormLayout1_txtUsername_I", self.creds.username)
        self.page.fill("#ASPxFormLayout1_txtPassword_I", self.creds.password)

        if not self._submit_login():
            raise RuntimeError(
                "Could not submit the login form. The portal's button layout may "
                "have changed. Run with headless=false in config.json to watch."
            )

        if not self._looks_logged_in():
            current = self.page.url or ""
            if "target=" in current and "login.aspx" in current.lower():
                # Portal bounced us to a "session conflict" / "other session
                # active" screen. Likely cause: the same account is logged in
                # from another browser (e.g. a manual debug session).
                raise RuntimeError(
                    "Login bounced back to Login.aspx with a ?target= redirect "
                    "payload. This usually means another session for the same "
                    "account is already active (portal enforces single session). "
                    "Close any browser tabs where this account is logged in and "
                    "try again. If the problem persists, run with headless=false "
                    "to see the screen the portal is showing."
                )
            raise RuntimeError(
                "Login appears to have failed. Check credentials in credentials.json. "
                "If they're correct, the portal may require a CAPTCHA or a different flow. "
                f"Current URL: {_scrub(current)}"
            )
        log.info("Login OK (now at %s)", _scrub(self.page.url))

    def _submit_login(self) -> bool:
        """Try the cleanest login submit, then fall back to more forceful approaches.

        Returns True as soon as one of the attempts reaches a logged-in state.
        """
        attempts = [
            ("press Enter in password field",
             lambda: self.page.press("#ASPxFormLayout1_txtPassword_I", "Enter")),
            ("invoke DevExpress btnLogin.DoClick()",
             lambda: self.page.evaluate(
                 "() => {"
                 " var c = window.ASPxClientControl && ASPxClientControl"
                 "        .GetControlCollection().GetByName('ASPxFormLayout1$btnLogin');"
                 " if (c && c.DoClick) c.DoClick();"
                 "}")),
            ("click DevExpress button-text overlay",
             lambda: self.page.locator("#ASPxFormLayout1_btnLogin_BTC").click(
                 force=True, timeout=6000)),
            ("force-click the real submit input",
             lambda: self.page.locator("#ASPxFormLayout1_btnLogin_I").click(
                 force=True, timeout=6000)),
            ("__doPostBack('ASPxFormLayout1$btnLogin', '')",
             lambda: self.page.evaluate(
                 "() => { if (typeof __doPostBack === 'function') "
                 "__doPostBack('ASPxFormLayout1$btnLogin', ''); }")),
        ]
        for label, action in attempts:
            log.info("Login submit attempt: %s", label)
            try:
                with self.page.expect_navigation(
                    wait_until="domcontentloaded", timeout=12000
                ):
                    action()
                log.info("  -> navigation occurred")
                return True
            except PlaywrightTimeout:
                # No nav yet — maybe an in-place AJAX update. Check if we're logged in.
                try:
                    self.page.wait_for_load_state("networkidle", timeout=4000)
                except PlaywrightTimeout:
                    pass
                if self._looks_logged_in():
                    log.info("  -> page looks logged in (no explicit nav)")
                    return True
            except Exception as e:  # noqa: BLE001
                log.debug("  -> attempt failed: %s", e)
                continue
        return False

    def _looks_logged_in(self) -> bool:
        """Authoritative login check.

        The portal's chrome (nav menu etc.) shows "My Request Center" links
        even on Login.aspx / session-conflict pages, so DOM-only checks give
        false positives. We require either:
          - the URL is already on a customer/request page (never Login.aspx), or
          - the request list container is actually rendered on the page AND
            we are not on Login.aspx.
        """
        url = (self.page.url or "").lower()
        # If we're still sitting on Login.aspx for any reason (initial load,
        # session-conflict bounce, target= redirect screen), we're NOT logged in.
        if "login.aspx" in url:
            return False
        if "customerissues" in url or "customerhome" in url:
            return True
        try:
            if self.page.locator("#roundPanel_issuesListView").count() > 0:
                return True
        except Exception:
            pass
        return False

    # -- listing ---------------------------------------------------------------
    def goto_request_center(self) -> None:
        """Navigate to the request list page (CustomerIssues.aspx).

        The post-login landing page is CustomerHome.aspx (with a sSessionID
        query param appended). We preserve that query string on the direct
        navigation because the portal sometimes validates it server-side —
        dropping it can bounce you back to Login.
        """
        if "CustomerIssues.aspx" in self.page.url:
            self._wait_for_list()
            return

        # Build a CustomerIssues.aspx URL with the same (S(sid)) path AND the
        # same ?sSessionID=... query string as wherever we are now.
        target = re.sub(r"/[^/]+\.aspx(\?.*)?$",
                        lambda m: "/CustomerIssues.aspx" + (m.group(1) or ""),
                        self.page.url)
        log.info("Navigating to request list: %s", _scrub(target))
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=20000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            if self._list_present():
                return
            log.warning("After direct navigation, list container not present. "
                        "URL now: %s", _scrub(self.page.url))
        except PlaywrightTimeout:
            log.warning("Direct navigation to CustomerIssues.aspx timed out")
        except Exception as e:
            log.warning("Direct navigation failed: %s", e)

        # Fallback: click any menu link to the request center.
        for sel in (
            'a:has-text("My Request Center")',
            'a:has-text("My Requests")',
            'a[href*="CustomerIssues"]',
        ):
            link = self.page.locator(sel).first
            if link.count() == 0:
                continue
            log.info("Trying menu link: %s", sel)
            try:
                with self.page.expect_navigation(
                    wait_until="domcontentloaded", timeout=15000
                ):
                    link.click(force=True)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass
                if self._list_present():
                    return
            except PlaywrightTimeout:
                continue
            except Exception as e:
                log.debug("  -> link click failed: %s", e)
                continue

        # Last resort: raise with useful diagnostic info.
        self._wait_for_list()

    def _list_present(self) -> bool:
        for sel in ("#roundPanel_issuesListView",
                    '[id*="issuesListView"]',
                    'a[id^="roundPanel_issuesListView_ctrl"][id$="_referenceLnk"]'):
            try:
                if self.page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False

    def _wait_for_list(self) -> None:
        try:
            self.page.wait_for_selector("#roundPanel_issuesListView", timeout=15000)
        except PlaywrightTimeout as e:
            # Collect diagnostics so the user can tell whether we landed on the
            # wrong page, lost the session, or the container was simply renamed.
            try:
                info = self.page.evaluate(
                    """
                    () => {
                      const ids = Array.from(document.querySelectorAll('[id]'))
                        .map(el => el.id)
                        .filter(id =>
                          /issues|request|listView|Panel/i.test(id)
                        )
                        .slice(0, 25);
                      return {
                        url:   window.location.href,
                        title: document.title,
                        h1:    (document.querySelector('h1') || {}).innerText || '',
                        relatedIds: ids,
                      };
                    }
                    """
                ) or {}
            except Exception:
                info = {}
            raise RuntimeError(
                "Request list (#roundPanel_issuesListView) did not render.\n"
                f"  url:   {_scrub(info.get('url') or self.page.url)}\n"
                f"  title: {info.get('title')!r}\n"
                f"  h1:    {info.get('h1')!r}\n"
                f"  related ids on page: {info.get('relatedIds') or []}\n"
                "If 'url' above is a Login page, session wasn't carried over. "
                "If the URL looks right but no related IDs are listed, the "
                "list container has been renamed — send me the page source."
            ) from e

    def iter_all_request_summaries(self) -> Iterator[RequestSummary]:
        """Yield every request across all pagination pages. Read-only."""
        visited_pages = 0
        while True:
            visited_pages += 1
            if visited_pages > self.config.max_pages_to_scrape:
                log.warning(
                    "Stopping: reached max_pages_to_scrape=%d",
                    self.config.max_pages_to_scrape,
                )
                return

            summaries = self._parse_current_listing_page()
            log.info("Page %d: %d requests", visited_pages, len(summaries))
            for s in summaries:
                yield s

            if not self._goto_next_page():
                log.info("No more pages after page %d", visited_pages)
                return
            human_delay.sleep_between_pages(self.config.human_delay_pages)

    def iter_new_request_summaries(
        self, is_known
    ) -> Iterator[RequestSummary]:
        """Yield only request summaries not already known to the caller.

        The list is sorted newest-first, so once we hit a summary where
        `is_known(summary)` is True, we stop paginating entirely — anything
        past that point has already been recorded.

        Returns early on the last page just like `iter_all_request_summaries`.
        The known-summary that triggers the stop is NOT yielded.

        Use this for efficient incremental runs. Callers who also want to
        refresh in-DB open requests should do that separately (they may be
        buried many pages deep, past the stop-point).
        """
        visited_pages = 0
        yielded_new = 0
        while True:
            visited_pages += 1
            if visited_pages > self.config.max_pages_to_scrape:
                log.warning(
                    "Stopping: reached max_pages_to_scrape=%d",
                    self.config.max_pages_to_scrape,
                )
                return

            summaries = self._parse_current_listing_page()
            log.info("Page %d: %d requests", visited_pages, len(summaries))
            for s in summaries:
                if is_known(s):
                    log.info(
                        "Hit already-known request %s; stopping list walk "
                        "(%d new summaries yielded).",
                        s.request_id, yielded_new,
                    )
                    return
                yield s
                yielded_new += 1

            if not self._goto_next_page():
                log.info("No more pages after page %d", visited_pages)
                return
            human_delay.sleep_between_pages(self.config.human_delay_pages)

    def _parse_current_listing_page(self) -> list[RequestSummary]:
        """Pull structured data from the 5-per-page ListView cards."""
        items = self.page.evaluate(
            """
            () => {
              const cards = Array.from(document.querySelectorAll(
                'a[id^="roundPanel_issuesListView_ctrl"][id$="_referenceLnk"]'
              ));
              return cards.map(a => {
                // card ids are like "roundPanel_issuesListView_ctrl0_referenceLnk"
                const idxMatch = a.id.match(/_ctrl(\\d+)_referenceLnk$/);
                const idx = idxMatch ? idxMatch[1] : '';
                const pfx = `roundPanel_issuesListView_ctrl${idx}_`;
                const get = (suffix) => {
                  const el = document.getElementById(pfx + suffix);
                  return el ? (el.innerText || '').trim() : '';
                };
                // Try to read the real Details URL from the card:
                //   1) the reference link itself (if it has an href)
                //   2) the Details button/anchor if present
                //   3) any other anchor in the card whose href references RequestEdit.aspx
                function extractUrl(el) {
                  if (!el) return null;
                  const h = el.getAttribute && el.getAttribute('href');
                  if (h && /RequestEdit/i.test(h)) return h;
                  const oc = el.getAttribute && el.getAttribute('onclick');
                  if (oc) {
                    const m = oc.match(/['"]((?:[^'"]*?)RequestEdit\\.aspx[^'"]*)['"]/i);
                    if (m) return m[1];
                  }
                  return null;
                }
                const details = document.getElementById(pfx + 'btnDetails_I');
                const card = a.closest('.mainbg, .dxflCard, li, tr, .dxrp') || a.parentElement;
                let detailUrl =
                  extractUrl(a) || extractUrl(details);
                if (!detailUrl && card) {
                  const anchors = Array.from(card.querySelectorAll('a[href]'));
                  for (const aa of anchors) {
                    const u = extractUrl(aa);
                    if (u) { detailUrl = u; break; }
                  }
                }
                return {
                  ref: (a.innerText || '').trim(),
                  requester: get('primaryCustomer'),
                  stage:    get('divHyperlink'),
                  detail_url: detailUrl,
                };
              });
            }
            """
        )
        summaries: list[RequestSummary] = []
        for item in items:
            ref = (item.get("ref") or "").strip()
            if not REF_PATTERN.fullmatch(ref):
                # Defensive: skip malformed entries
                continue
            try:
                rid = int(ref.split("-", 1)[0].lstrip("P"))
            except ValueError:
                continue
            stage = (item.get("stage") or "").strip() or None
            # Prefer the portal's own detail URL if we could extract one; fall
            # back to constructing ?rid={numeric prefix}, which is the same URL
            # pattern the portal itself uses.
            portal_url = item.get("detail_url")
            if portal_url:
                detail_url = self._absolutize(portal_url)
            else:
                detail_url = self._detail_url_for(rid)
            summaries.append(RequestSummary(
                request_id=ref,
                rid=rid,
                status=stage,            # progress stage is the visible "status"
                final_state=stage,       # used for closed-request detection
                detail_url=detail_url,
            ))
        if not summaries:
            # Zero cards parsed. If the list container IS present, this is a
            # render race / partial load / changed card-id scheme — NOT a
            # legitimately empty portal. Warn loudly (but don't hard-error, so a
            # genuinely empty/new account still completes cleanly).
            try:
                present = self._list_present()
            except Exception:
                present = False
            if present:
                log.warning(
                    "Listing container present but zero request cards parsed — "
                    "possible render race or changed card-id scheme. URL: %s",
                    _scrub(self.page.url),
                )
        return summaries

    def _absolutize(self, url: str) -> str:
        """Resolve a possibly-relative detail URL against the current page."""
        if not url:
            return url
        # Already absolute?
        if re.match(r"^https?://", url):
            return url
        # Starts with "/" → host-relative
        if url.startswith("/"):
            m = re.match(r"^(https?://[^/]+)", self.page.url)
            if m:
                return m.group(1) + url
            return url
        # Otherwise treat as relative to the directory of the current URL
        base_dir = re.sub(r"[^/]*$", "", self.page.url.split("?")[0])
        return base_dir + url

    def _detail_url_for(self, rid: int) -> str:
        """Build a RequestEdit.aspx URL that preserves the session path prefix."""
        base = self.page.url.split("?")[0]
        base = re.sub(r"/[^/]+\.aspx$", "/RequestEdit.aspx", base)
        return f"{base}?rid={rid}"

    def _goto_next_page(self) -> bool:
        """Advance the DataPager to the next page.

        Returns True if we advanced (current page strictly increased),
        False if we're already on the last page.

        Portal pager mechanics (verified 2026-04-20 on live portal):
        - Anchors: «=First (ctl00$ctl00), numeric page links (ctl01$ctlNN),
          …=jump (ctl01$ctlNN), »=Last (ctl02$ctl00). No dedicated Next.
        - Current page is a plain <span>N</span>; anchors for every other
          visible page position.
        - The pager only shows ~4 numeric anchors at a time and they are
          NOT guaranteed to be centered on the current page. Example
          actually observed: on page 5 of 29, numerics were 1,2,3,4, with
          '…' jumping forward. So N+1 may not be in the numeric anchors.
        - '…' can appear as a BACK jump (before the numerics, e.g. on the
          last page) or a FORWARD jump (after the numerics). We only use
          the forward one — identified by DOM position after the last
          numeric anchor and before »/end.
        - After clicking, the portal does a full form-submit navigation
          (the DataPager is NOT in an UpdatePanel). We use
          `expect_navigation` to wait, then verify the active page
          actually increased (in case '…' jumped somewhere unexpected).
        """
        info = self._read_pager()
        if info is None:
            log.info("No pager on current listing page.")
            return False

        cur = info["current"]
        items = info["items"]

        pick = self._choose_next_pager_item(cur, items)
        if pick is None:
            # Genuinely the last page — the ONLY clean "stop paginating" signal.
            log.info(
                "Pagination: end of list (current=%s, visible=%s)",
                cur,
                [it.get("text") for it in items if not it.get("disabled")],
            )
            return False

        # We DO have a next page to go to. A failed/non-advancing postback here
        # must NOT be silently treated as "last page" (that truncates the walk
        # and silently drops later pages). Retry once with a short fixed backoff,
        # and if it still won't advance, log a loud WARNING before giving up.
        for attempt in range(1, 3):
            target = pick.get("target")
            if not target:
                log.warning("Chosen pager anchor has no __doPostBack target: %s", pick)
                return False
            log.info("Pagination: %s -> (%s) (target=%s, attempt %d/2)",
                     cur, pick.get("text"), target, attempt)
            if self._postback_and_wait_pager(target):
                new_info = self._read_pager()
                new_cur = new_info["current"] if new_info else None
                if new_cur is not None and new_cur > cur:
                    if new_cur > cur + 1:
                        log.info("Pagination jumped %s -> %s ('...' forward jump).",
                                 cur, new_cur)
                    return True
                log.warning("Pagination did not advance (was %s, now %s); attempt %d/2.",
                            cur, new_cur, attempt)
            else:
                log.warning("Pager postback failed; attempt %d/2.", attempt)
            if attempt < 2:
                time.sleep(3)  # fixed backoff, NOT human_delay (pacing unaffected)
                info = self._read_pager()
                if info is None:
                    break
                cur = info["current"]
                pick = self._choose_next_pager_item(cur, info["items"])
                if pick is None:
                    return False  # re-read shows we're genuinely on the last page
        log.warning(
            "Pagination could not advance past page %s after retry — stopping "
            "the list walk EARLY; requests on later pages may be missed this run.",
            cur,
        )
        return False

    def _read_pager(self) -> dict | None:
        """Snapshot the pager: current page number and every anchor's position,
        text, target, disabled-flag."""
        info = self.page.evaluate(
            """
            () => {
              const pager = document.getElementById('roundPanel_issuesListView_listDataPager');
              if (!pager) return null;
              // Walk pager children in DOM order; track whether each element is
              // an anchor (nav) or the current-page span.
              const items = [];
              let current = null;
              let domPos = 0;
              const walk = (el) => {
                for (const child of el.children) {
                  if (child.tagName === 'A') {
                    const href = child.getAttribute('href') || '';
                    const m = href.match(/__doPostBack\\('([^']+)'/);
                    items.push({
                      domPos: domPos++,
                      text: (child.innerText || '').trim(),
                      cls: child.className || '',
                      target: m ? m[1] : null,
                      disabled: (child.className || '').includes('aspNetDisabled')
                    });
                  } else if (child.tagName === 'SPAN') {
                    const t = (child.innerText || '').trim();
                    if (/^\\d+$/.test(t) && current === null) current = parseInt(t, 10);
                    walk(child);  // descend into wrapping spans
                  } else {
                    walk(child);
                  }
                }
              };
              walk(pager);
              return { current, items };
            }
            """
        )
        if not info or info.get("current") is None:
            return None
        return info

    def _choose_next_pager_item(self, cur: int, items: list[dict]) -> dict | None:
        """Pick the anchor that most likely advances us one step forward.

        Strategy:
          1. Numeric anchor whose text equals str(cur+1)  — preferred (advance by 1).
          2. Smallest numeric anchor whose text > cur     — advance by more than 1.
          3. '…' anchor positioned AFTER all numeric      — forward group jump.
        """
        WANTED = str(cur + 1)

        # Strategy 1 & 2: numeric anchors > cur
        numeric: list[tuple[int, dict]] = []
        for it in items:
            if it.get("disabled"):
                continue
            txt = it.get("text") or ""
            if txt.isdigit():
                n = int(txt)
                if n > cur:
                    numeric.append((n, it))

        for n, it in numeric:
            if str(n) == WANTED:
                return it
        if numeric:
            numeric.sort(key=lambda x: x[0])
            return numeric[0][1]

        # Strategy 3: '…' forward jump (ellipsis positioned AFTER the last
        # numeric anchor in DOM order).
        JUMP_GLYPHS = ("...", "…")
        last_numeric_pos = -1
        for it in items:
            if (it.get("text") or "").isdigit():
                last_numeric_pos = max(last_numeric_pos, it.get("domPos", -1))
        for it in items:
            if it.get("disabled"):
                continue
            if it.get("text") in JUMP_GLYPHS and it.get("domPos", -1) > last_numeric_pos:
                return it

        return None

    def _postback_and_wait_pager(self, target: str) -> bool:
        """Fire __doPostBack(target,''), wait for full-page nav to settle,
        and wait for the pager to re-render on the new page."""
        escaped_target = target.replace("\\", "\\\\").replace("'", "\\'")
        postback_js = (
            f"new Function(\"__doPostBack('{escaped_target}', '')\")()"
        )
        try:
            with self.page.expect_navigation(
                timeout=30000, wait_until="domcontentloaded"
            ):
                self.page.evaluate(postback_js)
        except PlaywrightTimeout:
            log.debug("No navigation after postback; treating as partial refresh.")
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "context was destroyed" not in msg and "navigating" not in msg:
                log.info("Pager postback failed: %s", e)
                return False
            log.debug("Evaluate race with navigation — proceeding.")

        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass
        try:
            self.page.wait_for_selector(
                "#roundPanel_issuesListView_listDataPager", timeout=15000
            )
        except PlaywrightTimeout:
            log.info("Pager did not reappear after postback.")
            return False
        return True

    def _wait_for_page_change(self) -> bool:
        # DataPager does an async postback; wait for network idle then confirm list present.
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass
        try:
            self.page.wait_for_selector("#roundPanel_issuesListView", timeout=10000)
        except PlaywrightTimeout:
            return False
        return True

    # -- detail page -----------------------------------------------------------
    def scrape_detail(self, summary: RequestSummary):
        """Visit a request detail page and return (fields, messages, attachments).

        `fields` is a flat dict with canonical keys (request_type, requester_email,
        status, reference_no, category, department, records_type, description,
        preferred_method).

        If the portal session has silently expired (detail navigation gets
        redirected to Login.aspx), we re-authenticate and retry once. If the
        page still has no identifying fields after that, we raise — better to
        fail loudly than write an empty row.
        """
        self._goto_detail(summary.detail_url, summary.rid, summary.request_id)

        # Expose the "View File(s)" / "View Message(s)" panels. Their content is
        # server-rendered inline (no AJAX fetch) so clicking just flips visibility,
        # but we click anyway to be safe against future layouts that might lazy-load.
        self._click_if_present("#btnViewFile_I")
        self._click_if_present("#btnViewMessage_I")

        fields = self._map_detail_fields(self._extract_detail_fields())
        messages = self._extract_messages()
        attachments = self._extract_attachments()

        # Defense-in-depth: if none of the anchor fields came back, the page
        # wasn't a real detail page (access denied, stale rid, partial load,
        # or a redirect we didn't catch). Don't persist an empty row — raise
        # so the caller skips the upsert.
        if not any(fields.get(k) for k in ("request_type", "reference_no", "status")):
            raise RuntimeError(
                f"Detail page for {summary.request_id} (rid={summary.rid}) "
                f"returned no identifying fields. Current URL: {self.page.url}"
            )
        return fields, messages, attachments

    def _goto_detail(self, detail_url: str, rid: int, request_id: str) -> None:
        """Navigate to a detail page, self-healing if the session has expired.

        Portal sessions can go stale mid-run (idle timeout, or another login
        elsewhere bumping us). A stale navigation silently lands on Login.aspx,
        which has none of the detail DOM — so field extraction quietly returns
        empty and we'd overwrite real data with blanks. We detect that here and
        re-authenticate once before retrying.

        The portal also redirects to `error.aspx?err_msg=Issue+Not+Found` when
        a rid doesn't correspond to an accessible request (never existed, was
        deleted, or belongs to a different account). We raise a clear
        `RequestNotFoundError` for those so the caller can distinguish a bad
        rid from a transient failure.
        """
        self._goto_with_retry(detail_url)

        err = self._portal_error_message()
        if err is not None:
            raise RequestNotFoundError(request_id, rid, err)

        if not self._on_login_page():
            return

        log.warning(
            "Session expired visiting %s (rid=%d) — re-authenticating and "
            "retrying once.", request_id, rid,
        )
        self.login()
        self.goto_request_center()
        # Rebuild the URL against the NEW session path prefix. The original
        # detail_url had the old (S(sid)) prefix which is now invalid.
        retry_url = self._detail_url_for(rid)
        self._goto_with_retry(retry_url)

        err = self._portal_error_message()
        if err is not None:
            raise RequestNotFoundError(request_id, rid, err)
        if self._on_login_page():
            raise RuntimeError(
                f"Still bouncing to login after re-auth — cannot reach detail "
                f"page for {request_id} (rid={rid})."
            )

    def _on_login_page(self) -> bool:
        """True if the current page URL looks like the portal's login screen."""
        url = (self.page.url or "").lower()
        return "login.aspx" in url

    def _goto_with_retry(self, url: str, attempts: int = 2) -> None:
        """Navigate (read-only GET) with a small bounded retry on transient
        timeouts. Given the long human delays between records, a sub-minute
        network blip is far likelier than the page being gone, so one cheap
        retry avoids skipping a request for the whole run. Uses a short FIXED
        sleep — NOT human_delay — so anti-bot pacing is unaffected."""
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                try:
                    self.page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeout:
                    pass
                return
            except PlaywrightTimeout as e:
                last_exc = e
                log.warning("Navigation to %s timed out (attempt %d/%d).",
                            _scrub(url), i + 1, attempts)
                if i + 1 < attempts:
                    time.sleep(3)
        if last_exc is not None:
            raise last_exc

    def _portal_error_message(self) -> str | None:
        """If the current page is the portal's error redirect, return the
        `err_msg` query value (e.g. 'Issue Not Found'). Otherwise None."""
        from urllib.parse import urlparse, parse_qs, unquote_plus
        url = self.page.url or ""
        parsed = urlparse(url)
        if not parsed.path.lower().endswith("/error.aspx"):
            return None
        qs = parse_qs(parsed.query)
        msg = qs.get("err_msg", [""])[0]
        return unquote_plus(msg) if msg else "unknown error"

    def _click_if_present(self, css: str) -> None:
        """Force-click a DevExpress button by id, if present.

        We use Playwright's native click rather than JS eval of
        ASPxClientControl.DoClick(), because DoClick may invoke __doPostBack,
        which fails in Playwright's strict-mode eval context (it accesses
        arguments.callee inside Sys.WebForms.PageRequestManager). Native
        clicks run the href/handler in the page's own (sloppy) context.
        """
        try:
            loc = self.page.locator(css).first
            if loc.count() == 0:
                return
            try:
                loc.click(force=True, timeout=5000)
            except Exception as e:
                log.debug("  click %s failed: %s", css, e)
                return
            # Short wait for any AJAX/DOM changes to settle
            try:
                self.page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeout:
                pass
        except Exception as e:
            log.debug("  click %s skipped: %s", css, e)

    def _extract_detail_fields(self) -> dict:
        return self.page.evaluate(
            """
            () => {
              function txt(id) {
                const el = document.getElementById(id);
                return el ? (el.innerText || '').trim() : null;
              }
              // Pull "Additional Information" rows by caption-text → value
              const captionMap = {};
              const root = document.getElementById('requestData_CustomFieldsFormLayout');
              if (root) {
                const caps = root.querySelectorAll('span.dxflCaption_Moderno');
                caps.forEach(cap => {
                  const label = (cap.innerText || '').trim()
                    .replace(/:$/, '').toLowerCase();
                  const capCell = cap.closest('td');
                  const tr = capCell ? capCell.parentElement : null;
                  const cells = tr ? Array.from(tr.children) : [];
                  const idx = cells.indexOf(capCell);
                  const valCell = (idx >= 0 && cells[idx + 1]) ? cells[idx + 1] : null;
                  if (!valCell) return;
                  // The value is either a span or the cell text itself
                  const vEl = valCell.querySelector(
                    'span[id^="requestData_CustomFieldsFormLayout_cf_"], ' +
                    'input[type=text], textarea, div[id$="TextContainer"]'
                  );
                  const value = vEl
                    ? (vEl.tagName === 'INPUT' || vEl.tagName === 'TEXTAREA'
                       ? vEl.value : vEl.innerText)
                    : valCell.innerText;
                  captionMap[label] = (value || '').trim();
                });
              }
              return {
                request_type:     txt('RequestEditFormLayout_roType'),
                requester_email:  txt('RequestEditFormLayout_roContactEmail'),
                reference_no:     txt('RequestEditFormLayout_roReferenceNo'),
                status:           txt('RequestEditFormLayout_roStatus'),
                _captions:        captionMap
              };
            }
            """
        ) or {}

    def _map_detail_fields(self, raw: dict) -> dict:
        """Merge the caption-keyed custom fields into the canonical field dict."""
        caps = raw.pop("_captions", {}) or {}
        for label, val in caps.items():
            key = CUSTOM_FIELD_LABEL_MAP.get(label)
            if key and val:
                raw[key] = val
        return raw

    def _extract_messages(self) -> list[MessageRecord]:
        raw = self.page.evaluate(
            """
            () => {
              const panels = Array.from(document.querySelectorAll(
                'table[id*="rptMessageHistory"][id*="__pnlMessages_"]'
              ));
              return panels.map(t => {
                const header = t.querySelector('td.dxrpHeader_Moderno');
                const body = t.querySelector('td.dxrp.dxrpcontent');
                return {
                  id: t.id,
                  header_text: header ? (header.innerText || '').trim() : '',
                  body_text:   body   ? (body.innerText   || '').trim() : '',
                };
              });
            }
            """
        ) or []
        messages: list[MessageRecord] = []
        for idx, item in enumerate(raw):
            m = PANEL_MSG_ID_PATTERN.search(item.get("id") or "")
            if not m:
                continue
            msg_id = int(m.group(1))
            header = item.get("header_text") or ""
            body = item.get("body_text") or ""
            hdr = MESSAGE_HEADER_PATTERN.search(header)
            if hdr:
                try:
                    sent_at = dateparser.parse(hdr.group(1)).isoformat(timespec="seconds")
                except (ValueError, OverflowError):
                    sent_at = hdr.group(1)
                sender = hdr.group(2).strip()
            else:
                # Header didn't match "On <date>, <sender> wrote:". Don't store
                # the raw blob as the timestamp/sender — that corrupts sort order
                # and sender classification (and could masquerade as support).
                # Fail safe + warn. (sent_at is NOT NULL, so use "" not None.)
                log.warning("Unparseable message header (msg %s): %r",
                            msg_id, header[:200])
                sent_at = ""
                sender = "unparsed"
            subject = None
            sub_m = re.search(r"^Subject:\s*(.+)$", body, re.MULTILINE)
            if sub_m:
                subject = sub_m.group(1).strip().splitlines()[0]
            is_auto_ack = self._looks_automated(subject, body, sender)
            messages.append(MessageRecord(
                message_id=msg_id,
                sent_at=sent_at,
                sender=sender,
                subject=subject,
                body=body,
                sequence_num=idx + 1,
                is_auto_ack=is_auto_ack,
            ))
        # DOM order is newest-first; re-sort chronologically so sequence_num is stable.
        messages.sort(key=lambda r: r.sent_at)
        for i, mr in enumerate(messages, start=1):
            mr.sequence_num = i
        return messages

    @staticmethod
    def _looks_automated(subject: str | None, body: str, sender: str) -> bool:
        if "support" not in (sender or "").lower():
            return False
        haystack = f"{subject or ''}\n{body}".lower()
        keywords = (
            "welcome to the records center",
            "thank you for your interest in public records",
            "your request has been received",
            "request created on public portal",
        )
        return any(k in haystack for k in keywords)

    def _extract_attachments(self) -> list[AttachmentRecord]:
        raw = self.page.evaluate(
            """
            () => {
              const links = Array.from(document.querySelectorAll('a[onclick*="IsDownloadable"]'));
              return links.map(a => ({
                text: (a.textContent || '').trim(),
                onclick: a.getAttribute('onclick') || '',
                href: a.getAttribute('href') || ''
              }));
            }
            """
        ) or []
        result: list[AttachmentRecord] = []
        seen: set[int] = set()
        for item in raw:
            m = ATTACHMENT_ONCLICK_PATTERN.search(item.get("onclick") or "")
            if not m:
                continue
            attachment_id = int(m.group(2))
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            # Postback target from href: __doPostBack('rptAttachments$ctl##$lnkStreamCloud','')
            target_m = re.search(r"__doPostBack\('([^']+)'", item.get("href") or "")
            if not target_m:
                # Fall back: derive from onclick's first arg (the lnkStreamCloud id)
                link_id = m.group(1)
                target = link_id.replace("_", "$")
            else:
                target = target_m.group(1)
            result.append(AttachmentRecord(
                attachment_id=attachment_id,
                filename=item.get("text") or f"attachment_{attachment_id}",
                postback_target=target,
            ))
        return result

    # -- downloads -------------------------------------------------------------
    def download_attachment(self, rid: int, attachment: AttachmentRecord,
                            into_dir: Path) -> Path:
        """Trigger the ASP.NET postback to download a single attachment.

        The portal may show a DevExpress "Download Confirmation" popup for some
        files; we dismiss it by clicking its Yes/Confirm button if it appears.
        """
        into_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(attachment.filename)
        # Prefix with the globally-unique attachment_id so two attachments that
        # share a display name on the same request don't overwrite each other on
        # disk (silent data loss in a records-compliance tool). Existing files
        # are unaffected — their local_path is read verbatim from the DB.
        dest = into_dir / f"{attachment.attachment_id}_{safe_name}"

        # Make sure we're on the right detail page (postback targets are render-scoped).
        if "RequestEdit.aspx" not in self.page.url or f"rid={rid}" not in self.page.url:
            detail_url = self._detail_url_for(rid)
            self.page.goto(detail_url, wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            # Re-expose the attachments panel after navigating
            self._click_if_present("#btnViewFile_I")

        # Native click on the attachment anchor. We avoid page.evaluate(__doPostBack)
        # because Playwright's eval is strict mode and ASP.NET's postback code
        # reads arguments.callee. The <a> element's href is javascript:__doPostBack(
        # '{postback_target}', '') — a native click runs it in page context.
        target = attachment.postback_target  # e.g. "rptAttachments$ctl00$lnkStreamCloud"
        escaped = target.replace('"', '\\"')
        link_loc = self.page.locator(
            f'a[href*="__doPostBack(\'{escaped}\',"]'
        ).first
        if link_loc.count() == 0:
            # Fallback: match by onclick (IsDownloadable's first arg is the
            # underscored form of the postback target).
            underscored = target.replace("$", "_")
            link_loc = self.page.locator(
                f'a[onclick*="IsDownloadable(\\"{underscored}\\""]'
            ).first
        if link_loc.count() == 0:
            raise RuntimeError(
                f"Attachment link for postback target {target!r} not found on page"
            )

        try:
            with self.page.expect_download(
                timeout=self.config.download_timeout_seconds * 1000
            ) as download_info:
                link_loc.click(force=True, timeout=10000)
                # A DevExpress confirmation popup sometimes appears before download.
                self._maybe_confirm_download_popup()
            download: Download = download_info.value
        except PlaywrightTimeout as e:
            raise RuntimeError(
                f"Timeout waiting for download of {attachment.filename}"
            ) from e

        download.save_as(str(dest))
        return dest

    def _maybe_confirm_download_popup(self) -> None:
        """If a DevExpress 'Download Confirmation' popup appears, click its Yes
        button. Most files don't trigger it; the popup can also appear a beat
        after the click, so we briefly wait for it. A no-popup download just
        falls through silently (the wait times out and we move on)."""
        primary = ("#DownloadConfirmationControl_DownloadAllPopupPanel_"
                   "DownloadAllPopupFormLayout_DownloadAllPopupYes")
        try:
            try:
                self.page.wait_for_selector(primary, state="visible", timeout=2000)
            except PlaywrightTimeout:
                pass  # no popup for this file — the common, normal case
            yes = self.page.locator(primary).first
            if yes.count() > 0 and yes.is_visible():
                yes.click(force=True, timeout=2000)
                return
            # Generic fallback: any visible Yes/Confirm button inside a popup
            alt = self.page.locator(
                '[id*="DownloadConfirmation"] [id*="Yes"], '
                '[id*="DownloadConfirmation"] input[value="Yes"]'
            ).first
            if alt.count() > 0 and alt.is_visible():
                alt.click(force=True, timeout=2000)
        except Exception as e:  # noqa: BLE001
            log.debug("download confirmation handling skipped: %s", e)

# -- helpers -------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Strip characters that are awkward on Windows filesystems."""
    name = (name or "").strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = re.sub(r"_+", "_", name)
    return name[:200] or "unnamed_file"


def _scrub(s: str) -> str:
    """Best-effort redaction of session IDs before logging URLs."""
    return re.sub(r"\(S\([^)]+\)\)", "(S(sid))", s or "")
