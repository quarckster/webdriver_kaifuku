"""Core functionality for starting, restarting, and stopping a selenium browser."""
import atexit
import logging
import warnings

import attr

from selenium import webdriver
from selenium.common.exceptions import (
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.remote.file_detector import UselessFileDetector
from six.moves.urllib_error import URLError

from .tries import tries

log = logging.getLogger(__name__)


THIRTY_SECONDS = 30

BROWSER_ERRORS = URLError, WebDriverException
WHARF_OUTER_RETRIES = 2


class BrowserFactory(object):
    def __init__(self, webdriver_class, browser_kwargs):
        self.webdriver_class = webdriver_class
        self.browser_kwargs = browser_kwargs

        self._add_missing_options()

    def _add_missing_options(self):
        if self.webdriver_class is not webdriver.Remote:
            # desired_capabilities is only for Remote driver, but can sneak in
            self.browser_kwargs.pop("desired_capabilities", None)

    def processed_browser_args(self):
        self._add_missing_options()

        if "keep_alive" in self.browser_kwargs:
            warnings.warn(
                "forcing browser keep_alive to False due to selenium bugs\n"
                "we are aware of the performance cost and hope to redeem",
                category=RuntimeWarning,
            )
            return dict(self.browser_kwargs, keep_alive=False)
        return self.browser_kwargs

    def create(self):
        try:
            browser = tries(
                2,
                WebDriverException,
                self.webdriver_class,
                **self.processed_browser_args()
            )
        except URLError as e:
            if e.reason.errno == 111:
                # Known issue
                raise RuntimeError(
                    "Could not connect to Selenium server. Is it up and running?"
                )
            else:
                # Unknown issue
                raise

        browser.file_detector = UselessFileDetector()
        browser.maximize_window()
        return browser

    def close(self, browser):
        if browser:
            browser.quit()


class WharfFactory(BrowserFactory):
    def __init__(self, webdriver_class, browser_kwargs, wharf):
        super(WharfFactory, self).__init__(webdriver_class, browser_kwargs)
        self.wharf = wharf

        if (
            browser_kwargs.get("desired_capabilities", {}).get("browserName")
            == "chrome"
        ):
            # chrome uses containers to sandbox the browser, and we use containers to
            # run chrome in wharf, so disable the sandbox if running chrome in wharf
            co = browser_kwargs["desired_capabilities"].get("chromeOptions", {})
            arg = "--no-sandbox"
            if "args" not in co:
                co["args"] = [arg]
            elif arg not in co["args"]:
                co["args"].append(arg)
            browser_kwargs["desired_capabilities"]["chromeOptions"] = co

    def processed_browser_args(self):
        command_executor = self.wharf.config["webdriver_url"]
        view_msg = "tests can be viewed via vnc on display {}".format(
            self.wharf.config["vnc_display"]
        )
        log.info("webdriver command executor set to %s", command_executor)
        log.info(view_msg)
        return dict(
            super(WharfFactory, self).processed_browser_args(),
            command_executor=command_executor,
        )

    def create(self, url_key):
        def inner():
            try:
                self.wharf.checkout()
                return super(WharfFactory, self).create(url_key)
            except URLError as ex:
                # connection to selenum was refused for unknown reasons
                log.error(
                    "URLError connecting to selenium; recycling container. URLError:"
                )
                log.exception(ex)
                self.wharf.checkin()
                raise
            except Exception:
                log.exception("failure on webdriver usage, returning container")
                self.wharf.checkin()
                raise

        return tries(WHARF_OUTER_RETRIES, BROWSER_ERRORS, inner)

    def close(self, browser):
        try:
            super(WharfFactory, self).close(browser)
        finally:
            self.wharf.checkin()


@attr.s
class BrowserManager(object):
    browser_factory = attr.ib()
    browser = attr.ib(default=None, init=False)

    @classmethod
    def from_conf(cls, browser_conf):
        webdriver_name = browser_conf.get("webdriver", "Firefox")
        webdriver_class = getattr(webdriver, webdriver_name)

        browser_kwargs = browser_conf.get("webdriver_options", {})

        if "webdriver_wharf" in browser_conf:
            from .wharf import Wharf

            wharf = Wharf(browser_conf["webdriver_wharf"])
            atexit.register(wharf.checkin)
            if (
                browser_conf["webdriver_options"]["desired_capabilities"][
                    "browserName"
                ].lower()
                == "firefox"
            ):
                browser_kwargs["desired_capabilities"]["marionette"] = False
            return cls(WharfFactory(webdriver_class, browser_kwargs, wharf))
        else:
            if webdriver_name == "Remote":
                if (
                    browser_conf["webdriver_options"]["desired_capabilities"][
                        "browserName"
                    ].lower()
                    == "chrome"
                ):
                    browser_kwargs["desired_capabilities"]["chromeOptions"] = {}
                    browser_kwargs["desired_capabilities"]["chromeOptions"]["args"] = [
                        "--no-sandbox"
                    ]
                    browser_kwargs["desired_capabilities"].pop("marionette", None)
                if (
                    browser_conf["webdriver_options"]["desired_capabilities"][
                        "browserName"
                    ].lower()
                    == "firefox"
                ):
                    browser_kwargs["desired_capabilities"]["marionette"] = False

            return cls(BrowserFactory(webdriver_class, browser_kwargs))

    def _is_alive(self):
        log.debug("alive check")
        try:
            self.browser.current_url
        except UnexpectedAlertPresentException:
            # We shouldn't think that an Unexpected alert means the browser is dead
            return True
        except Exception:
            log.exception("browser in unknown state, considering dead")
            return False
        return True

    def ensure_open(self):
        if self._is_alive():
            return self.browser
        else:
            return self.start()

    def add_cleanup(self, callback):
        assert self.browser is not None
        try:
            cl = self.browser.__cleanup
        except AttributeError:
            cl = self.browser.__cleanup = []
        cl.append(callback)

    def _consume_cleanups(self):
        try:
            cl = self.browser.__cleanup
        except AttributeError:
            pass
        else:
            while cl:
                cl.pop()()

    def quit(self):
        self._consume_cleanups()
        try:
            self.factory.close(self.browser)
        except Exception as e:
            log.error("An exception happened during browser shutdown:")
            log.exception(e)
        finally:
            self.browser = None

    def start(self):
        if self.browser is not None:
            self.quit()
        return self.open_fresh()

    def open_fresh(self):
        log.info("starting browser")
        assert self.browser is None

        self.browser = self.factory.create()
        return self.browser
