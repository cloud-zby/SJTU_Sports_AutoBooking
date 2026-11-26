#!/usr/bin/env python3
"""
上海交通大学体育场馆自动预约脚本。

默认目标：
 - 学生中心
 - 学生中心健身房
 - 15:00-16:00
 - 任一可用场地
 - 日期：默认从最远日期往前遍历全部可预约日期；可用 --date 只预约指定的一天

人机验证：
 站点使用 AJ-Captcha，默认下发 clickWord（点选文字）。脚本会先把组件的
 captchaType 切换为 blockPuzzle（滑块），再用 OpenCV 模板匹配定位缺口，
 最后用合成鼠标事件拖动滑块，全过程无需人工干预（该切换仅影响前端组件，
 服务端两种验证类型都支持）。

仅监控模式（--only-moniter）：
 登录后按固定间隔只读地查询目标日期/时段是否还有可预约场地，
 一旦可预约就发邮件通知，全程不会选座、不会下单。
"""

import argparse
import base64
import hashlib
import os
import random
import re
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from PIL import Image
import pytesseract
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:  # 仅自动滑块验证需要，缺失时自动回退为手动
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None


BASE_URL = "https://sports.sjtu.edu.cn/pc/#/"
TARGET_VENUE_IDS = {
    "学生中心": "d784ad7c-cb24-4282-afd6-a67aec68c675",
    "子衿街学生活动中心": "768214ba-3b1c-4f29-ad00-15c0e376b000",
}
# 仅监控模式的默认收件人
DEFAULT_MAIL_TO = "xxc8865@gmail.com"


def _configure_console() -> None:
    """行缓冲 + 编码容错：页面里的 © 等字符在 GBK 控制台不再直接抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, errors="replace")
        except Exception:
            pass


_configure_console()

TIME_SLOTS = [
    "07:00-08:00",
    "08:00-09:00",
    "09:00-10:00",
    "10:00-11:00",
    "11:00-12:00",
    "12:00-13:00",
    "13:00-14:00",
    "14:00-15:00",
    "15:00-16:00",
    "16:00-17:00",
    "17:00-18:00",
    "18:00-19:00",
    "19:00-20:00",
    "20:00-21:00",
    "21:00-22:00",
]


@dataclass
class SubmitResult:
    status: str
    message: str
    order_id: str = ""


def init_driver(
    chromedriver_path: Optional[str],
    headless: bool = False,
    user_data_dir: Optional[str] = None,
    chrome_logs: bool = False,
) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # 静音 Chromium 原生日志（如 SharedImageManager::ProduceSkia 等 GPU 报错），
    # 这类日志只在关闭/重绘时出现，与脚本逻辑无关。
    options.add_argument("--log-level=3")
    options.add_argument("--disable-logging")
    options.add_argument("--silence-devtools-logs")
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")

    # 把 chromedriver 的 stderr 丢掉，避免 “DevTools listening on …” 等噪音；
    # Chrome 作为子进程会继承这个句柄，一并安静下来。
    log_output = None if chrome_logs else os.devnull
    if chromedriver_path:
        service = Service(chromedriver_path, log_output=log_output)
        driver = webdriver.Chrome(service=service, options=options)
    else:
        # Selenium Manager will choose/download a ChromeDriver matching the local Chrome.
        driver = webdriver.Chrome(
            options=options, service=Service(log_output=log_output)
        )

    driver.set_window_size(1400, 900)
    return driver


def wait_and_click(driver: webdriver.Chrome, xpath: str, timeout: int = 15):
    el = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xpath))
    )
    el.click()
    return el


def clean_captcha_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text or "")


def _safe_call(fn, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def focus_browser(driver: webdriver.Chrome) -> None:
    """把浏览器窗口切到最前面，方便用户手动完成人机验证。"""
    _safe_call(lambda: driver.switch_to.window(driver.current_window_handle))
    _safe_call(lambda: driver.maximize_window())
    _safe_call(
        lambda: driver.execute_script(
            "window.focus(); if (document.body) { document.body.focus(); }"
        )
    )


def describe_page_state(driver: webdriver.Chrome) -> Dict[str, Any]:
    """采集当前页面关键状态：URL、登录表单元素可见性、页面提示、登录 token。"""
    return _safe_call(
        lambda: driver.execute_script("""
            const info = (sel) => {
              const el = document.querySelector(sel);
              if (!el) return {found: false};
              const r = el.getBoundingClientRect();
              return {
                found: true,
                visible: r.width > 1 && r.height > 1,
                size: [Math.round(r.width), Math.round(r.height)]
              };
            };
            const messages = Array.from(
              document.querySelectorAll(
                '.el-message__content, .el-form-item__error, .error-tip, ' +
                '.login-error, .ant-message-notice-content'
              )
            ).map(x => (x.innerText || '').trim()).filter(Boolean);
            const warnBox = document.querySelector('#div_warn');
            const warnText = document.querySelector('#span_warn');
            const warnVisible = !!warnBox &&
              warnBox.getClientRects().length > 0 &&
              getComputedStyle(warnBox).display !== 'none';
            return {
              url: location.href,
              title: document.title,
              readyState: document.readyState,
              token: sessionStorage.getItem('token') || '',
              messages: messages,
              warnVisible: warnVisible,
              warnText: (warnText ? warnText.innerText : (warnBox ? warnBox.innerText : '')) || '',
              fields: {
                user: info('#input-login-user'),
                pass: info('#input-login-pass'),
                captchaImg: info('#captcha-img'),
                captchaInput: info('#input-login-captcha'),
                submit: info('#submit-password-button')
              },
              bodyText: ((document.body && document.body.innerText) || '').slice(0, 300)
            };
            """),
        {},
    )


def explain_login_failure(message: str) -> str:
    """把 JAccount 的提示文案（中/英文）翻译成可操作的建议。"""
    text = (message or "").lower()
    rules = [
        (
            ("验证码", "captcha"),
            "验证码识别错误：脚本会自动刷新页面换新验证码重试，连续失败后转人工输入",
        ),
        (
            ("锁定", "冻结", "停用", "locked", "disabled", "frozen", "suspended"),
            "账号可能被锁定/停用：请前往 jaccount.sjtu.edu.cn 处理",
        ),
        (
            ("频繁", "稍后", "frequent", "too many", "try again later"),
            "请求过于频繁：等几分钟后再试",
        ),
        (("过期", "超时", "expired", "timeout"), "页面/会话已过期：重新运行脚本即可"),
        (
            ("密码", "账号", "用户名", "username", "password", "account"),
            "账号或密码错误：请确认 --user/--password 是否正确",
        ),
    ]
    for keywords, hint in rules:
        if any(k in text for k in keywords):
            return hint
    return ""


def log_page_diagnostics(
    driver: webdriver.Chrome,
    tag: str,
    save_dir: str = ".",
    with_artifacts: bool = False,
) -> str:
    """打印当前页面诊断信息；with_artifacts=True 时额外保存截图与 HTML。

    返回页面上的错误提示文案（若有），便于调用方总结原因。
    """
    print(f"[诊断:{tag}] ----------------")
    print(
        f"[诊断:{tag}] 当前 URL: {_safe_call(lambda: driver.current_url, '无法读取')}"
    )

    alert_text = _safe_call(lambda: driver.switch_to.alert.text, "")
    if alert_text:
        print(f"[诊断:{tag}] 浏览器弹窗(alert): {alert_text}")
        _safe_call(lambda: driver.switch_to.alert.accept())

    state = describe_page_state(driver) or {}
    warn_text = ""
    messages: List[str] = []
    if not state:
        print(f"[诊断:{tag}] 无法读取页面状态（浏览器可能已退出）")
    else:
        print(
            f"[诊断:{tag}] title={state.get('title')!r} "
            f"readyState={state.get('readyState')} "
            f"token={'有' if state.get('token') else '无'}"
        )
        for name, info in (state.get("fields") or {}).items():
            info = info or {}
            found = "存在" if info.get("found") else "缺失"
            visible = "可见" if info.get("visible") else "不可见"
            print(
                f"[诊断:{tag}] 元素 {name}: {found}/{visible} size={info.get('size')}"
            )
        messages = state.get("messages") or []
        warn_text = (
            (state.get("warnText") or "").strip() if state.get("warnVisible") else ""
        )
        if messages:
            print(f"[诊断:{tag}] 页面提示信息: {'；'.join(messages)}")
        elif warn_text:
            print(f"[诊断:{tag}] 页面提示信息: {warn_text}")
        else:
            print(f"[诊断:{tag}] 页面提示信息: （无）")
        if warn_text and warn_text not in messages:
            print(f"[诊断:{tag}] JAccount 警告框(#div_warn): {warn_text}")
        body = (state.get("bodyText") or "").replace("\n", " | ").strip()
        if body:
            print(f"[诊断:{tag}] 页面文本: {body}")

    if with_artifacts:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(save_dir, f"debug-{tag}-{stamp}")
        png = base + ".png"
        try:
            driver.save_screenshot(png)
            print(f"[诊断:{tag}] 已保存截图: {png}")
        except Exception as exc:
            print(f"[诊断:{tag}] 保存截图失败: {type(exc).__name__}: {exc}")
        html = base + ".html"
        try:
            Path(html).write_text(driver.page_source or "", encoding="utf-8")
            print(f"[诊断:{tag}] 已保存 HTML: {html}")
        except Exception as exc:
            print(f"[诊断:{tag}] 保存 HTML 失败: {type(exc).__name__}: {exc}")
    print(f"[诊断:{tag}] ----------------")
    return alert_text or warn_text or "；".join(messages)


def is_sports_logged_in(driver: webdriver.Chrome) -> bool:
    if "sports.sjtu.edu.cn" not in driver.current_url:
        return False
    try:
        return bool(driver.execute_script("return sessionStorage.getItem('token')"))
    except Exception:
        return False


def click_school_login(driver: webdriver.Chrome) -> None:
    if "sports.sjtu.edu.cn" not in driver.current_url:
        driver.get(BASE_URL)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    if is_sports_logged_in(driver):
        return
    wait_and_click(driver, "//button[normalize-space()='校内人员登录']", timeout=20)


def wait_for_sports_login(driver: webdriver.Chrome, timeout: int) -> bool:
    try:
        WebDriverWait(driver, timeout).until(lambda d: is_sports_logged_in(d))
        return True
    except TimeoutException:
        return False


LOGIN_USER_CSS = "#input-login-user"
LOGIN_PASS_CSS = "#input-login-pass"
LOGIN_CAPTCHA_IMG_CSS = "#captcha-img"
LOGIN_CAPTCHA_INPUT_CSS = "#input-login-captcha"
LOGIN_SUBMIT_CSS = "#submit-password-button"


def looks_like_captcha_error(message: str) -> bool:
    """判断页面提示是不是“验证码错误”一类的问题（这类才值得刷新重试）。"""
    text = (message or "").lower()
    return any(keyword in text for keyword in ("验证码", "captcha", "verify code"))


def force_new_captcha(driver: webdriver.Chrome) -> None:
    """给验证码图片换个 URL 参数，绕开浏览器缓存拿到新图。"""
    _safe_call(lambda: driver.execute_script("""
            const img = document.querySelector('#captcha-img');
            if (!img) return;
            const src = img.getAttribute('src') || '';
            if (!src || src.startsWith('data:')) return;
            const base = src.split('?')[0];
            img.setAttribute('src', base + '?_=' + Date.now());
            """))


def refresh_login_page(driver: webdriver.Chrome, timeout: int = 20) -> bool:
    """刷新登录页以获取新的验证码；整页刷新失败时退回点击验证码图片。"""
    try:
        driver.refresh()
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, LOGIN_USER_CSS))
        )
        time.sleep(0.4)
        return True
    except Exception as exc:
        print(f"[登录] 刷新登录页失败: {type(exc).__name__}: {exc}")

    try:
        driver.find_element(By.CSS_SELECTOR, LOGIN_CAPTCHA_IMG_CSS).click()
        time.sleep(1.0)
        return True
    except Exception as exc:
        print(f"[登录] 点击验证码换图也失败: {type(exc).__name__}: {exc}")
    return False


def fill_credentials(driver: webdriver.Chrome, user: str, pwd: str) -> None:
    """填写账号与密码（不碰验证码）。"""
    user_input = driver.find_element(By.CSS_SELECTOR, LOGIN_USER_CSS)
    user_input.clear()
    user_input.send_keys(user)
    pass_input = driver.find_element(By.CSS_SELECTOR, LOGIN_PASS_CSS)
    pass_input.clear()
    pass_input.send_keys(pwd)


def read_captcha_text(driver: webdriver.Chrome, save_dir: str) -> tuple:
    """把验证码图片截下来做 OCR，返回 (识别文本, 图片字节哈希)。"""
    captcha_path = os.path.join(save_dir, "captcha.png")
    captcha_el = driver.find_element(By.CSS_SELECTOR, LOGIN_CAPTCHA_IMG_CSS)
    captcha_el.screenshot(captcha_path)
    raw = Path(captcha_path).read_bytes()
    digest = hashlib.md5(raw).hexdigest()
    size = _safe_call(lambda: Image.open(captcha_path).size)
    print(f"    验证码图片: {captcha_path} size={size}")
    with Image.open(captcha_path) as img:
        text = clean_captcha_text(pytesseract.image_to_string(img, config="--psm 7"))
    print(f"    验证码 OCR 结果: {text!r}")
    return text, digest


def manual_captcha_login(
    driver: webdriver.Chrome,
    user: str,
    pwd: str,
    save_dir: str,
    login_timeout: int,
) -> bool:
    """OCR 连续失败后，让用户在浏览器里手动输入验证码完成登录。"""
    print("=" * 60)
    print("[人工登录] 验证码自动识别已连续失败，转为人工输入。")
    print(f"[人工登录] 账号 {user} 会自动填好，你只需要填验证码并点登录。")
    print(f"[人工登录] 最长等待 {login_timeout} 秒，请不要关闭浏览器窗口。")
    print("=" * 60)

    try:
        if not driver.find_elements(By.CSS_SELECTOR, LOGIN_USER_CSS):
            refresh_login_page(driver)
        fill_credentials(driver, user, pwd)
        _safe_call(
            lambda: driver.find_element(
                By.CSS_SELECTOR, LOGIN_CAPTCHA_INPUT_CSS
            ).click()
        )
    except Exception as exc:
        print(f"[人工登录] 准备登录表单失败: {type(exc).__name__}: {exc}")
        log_page_diagnostics(driver, "manual-captcha-prepare", save_dir)
        return False

    focus_browser(driver)
    if wait_for_sports_login(driver, login_timeout):
        print("[人工登录] 登录成功")
        return True

    print(f"[人工登录] 等待超时（{login_timeout} 秒）")
    log_page_diagnostics(
        driver, "manual-captcha-timeout", save_dir, with_artifacts=True
    )
    return False


def login(
    driver: webdriver.Chrome,
    user: str,
    pwd: str,
    save_dir: str = ".",
    max_attempts: int = 5,
    manual_login: bool = False,
    login_timeout: int = 90,
    headless: bool = False,
    manual_fallback: bool = True,
) -> bool:
    if is_sports_logged_in(driver):
        print("已检测到预约平台登录态")
        return True

    print(
        f"[登录] 点击“校内人员登录”，起始 URL: {_safe_call(lambda: driver.current_url, '?')}"
    )
    try:
        click_school_login(driver)
    except Exception as exc:
        print(f"[登录] 点击“校内人员登录”失败: {type(exc).__name__}: {exc}")
        print(traceback.format_exc())
        log_page_diagnostics(
            driver, "click-login-failed", save_dir, with_artifacts=True
        )
        return False

    try:
        WebDriverWait(driver, 30).until(
            lambda d: "jaccount.sjtu.edu.cn" in d.current_url or is_sports_logged_in(d)
        )
    except TimeoutException:
        print("[登录] 等待跳转 JAccount 登录页超时（30 秒）")
        log_page_diagnostics(driver, "jaccount-timeout", save_dir, with_artifacts=True)
        return False

    print(f"[登录] 已跳转，当前 URL: {_safe_call(lambda: driver.current_url, '?')}")
    if is_sports_logged_in(driver):
        print("已通过已有 JAccount 会话进入预约平台")
        return True

    if manual_login:
        print("请在浏览器中手动完成 JAccount 登录...")
        if wait_for_sports_login(driver, login_timeout):
            return True
        print(f"[登录] 手动登录超时（{login_timeout} 秒）")
        log_page_diagnostics(
            driver, "manual-login-timeout", save_dir, with_artifacts=True
        )
        return False

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, LOGIN_USER_CSS))
        )
    except TimeoutException:
        print(f"[登录] 登录表单未出现（20 秒内找不到 {LOGIN_USER_CSS}）")
        log_page_diagnostics(driver, "form-missing", save_dir, with_artifacts=True)
        return False

    captcha_failures = 0
    last_captcha_digest = ""
    stalled_credentials = False

    for attempt in range(1, max_attempts + 1):
        if is_sports_logged_in(driver):
            print("[登录] 已进入预约平台，登录成功")
            return True
        if not driver.find_elements(By.CSS_SELECTOR, LOGIN_USER_CSS):
            # 登录表单消失通常说明上一次提交正在跳转，先等一会儿再下结论
            if wait_for_sports_login(driver, 20):
                print("[登录] 上一次提交已生效，登录成功")
                return True
            print(f"[登录] 表单消失但未进入预约平台，重新打开登录页（第 {attempt} 次）")
            try:
                click_school_login(driver)
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, LOGIN_USER_CSS))
                )
            except Exception as exc:
                print(f"[登录] 重新打开登录页失败: {type(exc).__name__}: {exc}")
                continue

        print(f"[尝试 {attempt}/{max_attempts}] 开始填写登录表单（账号: {user}）")
        try:
            fill_credentials(driver, user, pwd)
            time.sleep(0.4)

            text = ""
            digest = ""
            try:
                text, digest = read_captcha_text(driver, save_dir)
            except Exception as exc:
                print(f"    验证码识别异常: {type(exc).__name__}: {exc}")

            # 如果拿到的还是上一张图，说明刷新没生效，强制换图再识别一次
            if digest and digest == last_captcha_digest:
                print("    验证码图片未变化，强制刷新后重试识别")
                force_new_captcha(driver)
                time.sleep(1.0)
                try:
                    text, digest = read_captcha_text(driver, save_dir)
                except Exception as exc:
                    print(f"    验证码识别异常: {type(exc).__name__}: {exc}")
            last_captcha_digest = digest or last_captcha_digest

            if not text:
                captcha_failures += 1
                print(
                    f"[尝试 {attempt}] 验证码 OCR 未识别出内容"
                    f"（连续失败 {captcha_failures}/{max_attempts}），"
                    + (
                        "准备转人工输入"
                        if attempt >= max_attempts
                        else "刷新登录页后重试"
                    )
                )
                if attempt < max_attempts:
                    refresh_login_page(driver)
                continue

            captcha_input = driver.find_element(
                By.CSS_SELECTOR, LOGIN_CAPTCHA_INPUT_CSS
            )
            captcha_input.clear()
            captcha_input.send_keys(text)
            driver.find_element(By.CSS_SELECTOR, LOGIN_SUBMIT_CSS).click()
            print(f"[尝试 {attempt}] 已提交登录表单，等待登录结果...")

            if wait_for_sports_login(driver, 20):
                print(f"[尝试 {attempt}] 登录成功")
                return True

            print(
                f"[尝试 {attempt}] 登录未成功，当前 URL: "
                f"{_safe_call(lambda: driver.current_url, '?')}"
            )
            reason = log_page_diagnostics(
                driver, f"attempt{attempt}-failed", save_dir, with_artifacts=True
            )
            hint = explain_login_failure(reason)

            if looks_like_captcha_error(reason) or not reason:
                # 验证码错（或没有任何提示，基本也是验证码错）：刷新页面换个验证码
                captcha_failures += 1
                print(
                    f"[尝试 {attempt}] 判定为验证码识别错误"
                    f"（连续失败 {captcha_failures}/{max_attempts}）"
                )
                if hint:
                    print(f"[尝试 {attempt}] 推断原因: {hint}")
                if attempt < max_attempts:
                    refresh_login_page(driver)
                continue

            # 非验证码问题（密码错、账号锁定、请求过于频繁等），刷新验证码也没用
            if hint:
                print(f"[尝试 {attempt}] 推断原因: {hint}")
            stripped = (reason or "").lower()
            if any(
                keyword in stripped
                for keyword in (
                    "密码",
                    "账号",
                    "用户名",
                    "password",
                    "username",
                    "locked",
                )
            ):
                print(f"[尝试 {attempt}] 账号或密码问题，重试无效，终止登录")
                stalled_credentials = True
                break
            print(f"[尝试 {attempt}] 页面提示: {reason!r}，刷新后重试")
            refresh_login_page(driver)
        except Exception as exc:
            print(f"[尝试 {attempt}] 登录流程发生异常: {type(exc).__name__}: {exc}")
            print(traceback.format_exc())
            log_page_diagnostics(
                driver, f"attempt{attempt}-error", save_dir, with_artifacts=True
            )
            refresh_login_page(driver)

    if stalled_credentials:
        log_page_diagnostics(driver, "login-failed", save_dir, with_artifacts=True)
        return False

    print(f"自动识别验证码已连续失败 {captcha_failures} 次，仍未登录成功")
    log_page_diagnostics(driver, "login-failed", save_dir, with_artifacts=True)

    if not manual_fallback:
        print("[登录] 已禁用人工兜底（--no-manual-fallback）")
        return False
    if headless:
        print(
            "[登录] headless 模式下无法人工输入验证码；"
            "请去掉 --headless 后重跑，脚本会自动转到人工输入"
        )
        return False

    return manual_captcha_login(driver, user, pwd, save_dir, login_timeout)


def booking_url(venue_id: str, item_name: str) -> str:
    return f"{BASE_URL}apointmentDetails/1/{venue_id}/{quote(item_name)}/0"


def install_details_vm(driver: webdriver.Chrome) -> bool:
    return bool(driver.execute_script("""
            function findVue(vm, name) {
              if (!vm) return null;
              if (vm.$options && vm.$options.name === name) return vm;
              const children = vm.$children || [];
              for (let i = 0; i < children.length; i++) {
                const found = findVue(children[i], name);
                if (found) return found;
              }
              return null;
            }
            const app = document.querySelector('#app');
            const root = app && app.__vue__;
            const directEl = document.querySelector('#apointmentDetails');
            const direct = directEl && directEl.__vue__;
            window.__sjtu_vm = findVue(root, 'apointmentDetails') || direct || null;
            return !!window.__sjtu_vm;
            """))


def get_vm_snapshot(driver: webdriver.Chrome) -> Dict[str, Any]:
    return driver.execute_script("""
        if (!window.__sjtu_vm) return {};
        const vm = window.__sjtu_vm;
        return {
          venueName: (vm.detailedData && vm.detailedData.venueName) || '',
          activeType: vm.isactiveName || '',
          activeTypeId: vm.isactiveId || '',
          weekCount: (vm.weekList || []).length,
          fieldCount: (vm.topSite || []).length,
          seatColumnCount: (vm.seatArray || []).length,
          timeSlotCount: (function () {
            const a = vm.seatArray || [];
            let m = 0;
            for (let i = 0; i < a.length; i++) {
              const len = (a[i] || []).length;
              if (len > m) m = len;
            }
            return m;
          })(),
          loading: !!vm.loading,
          date: vm.timeData && vm.timeData.isdate,
          dateId: vm.dateId || '',
          messageText: document.body.innerText || ''
        };
        """)


def wait_for_details_ready(
    driver: webdriver.Chrome, timeout: int = 40
) -> Dict[str, Any]:
    def ready(d):
        if not install_details_vm(d):
            return False
        snapshot = get_vm_snapshot(d)
        if snapshot.get("venueName") and snapshot.get("weekCount", 0) > 0:
            return snapshot
        return False

    return WebDriverWait(driver, timeout).until(ready)


def pick_tab_name(names: List[str], item_name: str, venue: Optional[str] = None) -> str:
    """在项目（tab）名称中找出与 item_name 最匹配的一项；找不到返回空串。

    匹配顺序：精确 -> 去掉场馆名前缀（如 学生中心健身房 -> 健身房）
    -> 忽略大小写 -> 双向子串且唯一命中。
    """
    target = (item_name or "").strip()
    candidates = [n.strip() for n in names if n and n.strip()]
    if not target or not candidates:
        return ""

    if target in candidates:
        return target

    variants = [target]
    for prefix in (venue, "学生中心", "子衿街学生活动中心"):
        if prefix and target.startswith(prefix) and len(target) > len(prefix):
            variants.append(target[len(prefix) :])

    lowered = {n.lower(): n for n in candidates}
    for variant in variants:
        if variant in candidates:
            return variant
        hit = lowered.get(variant.lower())
        if hit:
            return hit

    fuzzy: List[str] = []
    for variant in variants:
        for name in candidates:
            if variant and (variant in name or name in variant) and name not in fuzzy:
                fuzzy.append(name)
    if len(fuzzy) == 1:
        return fuzzy[0]
    return ""


def select_motion_type(
    driver: webdriver.Chrome, item_name: str, venue: Optional[str] = None
) -> str:
    """切换到指定运动项目，返回实际选中的项目名（可能是模糊匹配结果）。"""
    tabs = None
    deadline = time.time() + 20
    while time.time() < deadline:
        tabs = driver.execute_script("""
            const vm = window.__sjtu_vm;
            if (!vm) return null;
            return (vm.tabList || []).map(x => ({name: x.name, id: x.id}));
            """)
        if tabs:
            break
        time.sleep(0.5)

    if tabs is None:
        raise RuntimeError(
            "未找到详情页 Vue 实例（window.__sjtu_vm），页面可能未加载完成"
        )
    if not tabs:
        raise RuntimeError("当前场馆没有任何可选项目（tabList 为空）")

    names = [t.get("name") for t in tabs if t.get("name")]
    matched = pick_tab_name(names, item_name, venue)
    if not matched:
        raise RuntimeError(
            f"未找到项目 {item_name!r}；该场馆可选项目: {', '.join(names)}"
        )
    if matched != item_name:
        print(
            f"项目名 {item_name!r} 未精确匹配，自动使用 {matched!r}"
            f"（可选项目: {', '.join(names)}）"
        )

    selected = driver.execute_script(
        """
        const vm = window.__sjtu_vm;
        const targetName = arguments[0];
        if (!vm) return {ok: false, reason: 'Vue instance not found'};
        if (vm.isactiveName === targetName) return {ok: true, changed: false};
        const match = (vm.tabList || []).find(x => x.name === targetName);
        if (!match) {
          return {ok: false, reason: '未找到项目: ' + targetName};
        }
        vm.handleClick({label: match.name, name: match.id});
        return {ok: true, changed: true};
        """,
        matched,
    )
    if not selected.get("ok"):
        raise RuntimeError(selected.get("reason", "选择项目失败"))
    if selected.get("changed"):
        time.sleep(1)
        WebDriverWait(driver, 20).until(
            lambda d: install_details_vm(d)
            and get_vm_snapshot(d).get("activeType") == matched
            and not get_vm_snapshot(d).get("loading")
        )
    return matched


def wait_for_field_loaded(
    driver: webdriver.Chrome, target_date: str, timeout: int = 25
) -> Dict[str, Any]:
    """等待指定日期的场地表格真正加载完成。

    注意：站点的 weekClick 会先把 seatArray 清空、并把 loading 置 false，
    再异步拉取场地数据，因此只判断 date + loading 会读到“空表格”。
    """

    def ready(d):
        if not install_details_vm(d):
            return False
        snapshot = get_vm_snapshot(d)
        if snapshot.get("date") != target_date:
            return False
        if snapshot.get("loading"):
            return False
        if int(snapshot.get("fieldCount") or 0) <= 0:
            return False
        if int(snapshot.get("seatColumnCount") or 0) <= 0:
            return False
        if int(snapshot.get("timeSlotCount") or 0) <= 0:
            return False
        return snapshot

    try:
        return WebDriverWait(driver, timeout).until(ready)
    except TimeoutException as exc:
        snapshot = get_vm_snapshot(driver)
        raise RuntimeError(
            f"{target_date} 的场地数据未加载（场地数={snapshot.get('fieldCount')}, "
            f"seatArray={snapshot.get('seatColumnCount')}x{snapshot.get('timeSlotCount')}, "
            f"loading={snapshot.get('loading')}）—— 可能该日期/项目暂不开放"
        ) from exc


def describe_seat_grid(driver: webdriver.Chrome, row_index: int) -> Dict[str, Any]:
    """输出场地表格诊断信息，便于定位“无可用场地”。"""
    return _safe_call(
        lambda: driver.execute_script(
            """
            const vm = window.__sjtu_vm;
            if (!vm) return {};
            const seatArray = vm.seatArray || [];
            const topSite = vm.topSite || [];
            const target = arguments[0];
            const statuses = {};
            for (let i = 0; i < seatArray.length; i++) {
              const row = seatArray[i] || [];
              for (let j = 0; j < row.length; j++) {
                const st = row[j] && row[j].status;
                statuses[st] = (statuses[st] || 0) + 1;
              }
            }
            const rowStatus = seatArray.map((row) => {
              const cell = (row || [])[target];
              return cell ? cell.status : null;
            });
            return {
              activeType: vm.isactiveName || '',
              nerData: vm.nerData || '',
              timeData: vm.timeData || {},
              fields: topSite.map((x) => x && x.name),
              fieldCount: topSite.length,
              outerCount: seatArray.length,
              innerCount: seatArray.reduce(
                (m, row) => Math.max(m, (row || []).length), 0
              ),
              statuses: statuses,
              rowStatus: rowStatus
            };
            """,
            row_index,
        ),
        {},
    )


def get_week_list(driver: webdriver.Chrome) -> List[Dict[str, Any]]:
    """返回详情页可预约日期列表：[{index, date, view, week}]"""
    result = driver.execute_script("""
        const vm = window.__sjtu_vm;
        if (!vm) return [];
        return (vm.weekList || []).map((item, idx) => ({
          index: idx,
          date: item.isdate || '',
          view: item.viewStr || '',
          week: item.week || ''
        }));
        """)
    return result or []


def extract_date_parts(value: str) -> Optional[tuple]:
    """从 '2026-11-26' / '2026/11/26 00:00:00' / '11月26日' 提取 (年|None, 月, 日)。"""
    text = str(value or "")
    match = re.search(r"(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", text)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})", text)
    if match:
        return (None, int(match.group(1)), int(match.group(2)))
    match = re.search(r"^\D*(\d{1,2})\D(\d{1,2})\D*$", text)
    if match:
        return (None, int(match.group(1)), int(match.group(2)))
    return None


def date_matches(item: Dict[str, Any], target_ymd: tuple) -> bool:
    """判断 weekList 条目是否与目标日期一致（同时比对 isdate 与 viewStr）。"""
    target_year, target_month, target_day = target_ymd
    for field in ("date", "view"):
        parts = extract_date_parts(item.get(field, ""))
        if not parts:
            continue
        year, month, day = parts
        if (month, day) != (target_month, target_day):
            continue
        if year is not None and target_year is not None and year != target_year:
            continue
        return True
    return False


def parse_date_arg(value: str) -> str:
    """把命令行日期解析成 YYYY-MM-DD；支持 today/tomorrow/YYYY-MM-DD/MM-DD。"""
    text = (value or "").strip()
    if not text:
        raise argparse.ArgumentTypeError("日期不能为空")
    lowered = text.lower()
    today = datetime.now().date()
    if lowered in {"today", "今天"}:
        return today.isoformat()
    if lowered in {"tomorrow", "明天"}:
        return (today + timedelta(days=1)).isoformat()

    normalized = text.replace("/", "-").replace(".", "-")
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            if fmt == "%m-%d":
                candidate = parsed.date().replace(year=today.year)
                # 跨年：如果算出来的日期已过去很久，视作明年
                if candidate < today - timedelta(days=180):
                    candidate = candidate.replace(year=today.year + 1)
                return candidate.isoformat()
            return parsed.date().isoformat()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        "日期格式应为 YYYY-MM-DD、MM-DD、today 或 tomorrow"
    )


def select_date(driver: webdriver.Chrome, index: int) -> Dict[str, Any]:
    result = driver.execute_script(
        """
        const vm = window.__sjtu_vm;
        const idx = arguments[0];
        const item = (vm.weekList || [])[idx];
        if (!item) return null;
        return {
          index: idx,
          date: item.isdate,
          view: item.viewStr,
          week: item.week,
          dateId: item.dateId || ''
        };
        """,
        index,
    )
    if not result:
        raise RuntimeError(f"日期索引无效: {index}")

    date_xpath = (
        "/html/body/div/div[2]/div[2]/div[2]/div[1]/div[2]/div[1]"
        f"/div/div/div/div[{index + 2}]/span/p"
    )
    try:
        date_el = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, date_xpath))
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            date_el,
        )
        time.sleep(0.2)
        try:
            date_el.click()
        except Exception:
            driver.execute_script("arguments[0].click();", date_el)
    except Exception as exc:
        print(f"日期 DOM 点击失败，尝试 Vue 方法切换: {exc}")
        # weekClick 期望传入带 name(=isdate) 的 Tab 对象，不能直接传 weekList 条目
        switched = driver.execute_script(
            """
            const vm = window.__sjtu_vm;
            const dateStr = arguments[0];
            if (!vm || !vm.weekClick) return false;
            const item = (vm.weekList || []).find((x) => x.isdate === dateStr);
            if (!item) return false;
            vm.weekClick({name: item.isdate});
            return true;
            """,
            result["date"],
        )
        if not switched:
            raise

    time.sleep(0.8)
    wait_for_field_loaded(driver, result["date"])
    return result


def available_seats(driver: webdriver.Chrome, row_index: int) -> List[Dict[str, Any]]:
    """返回目标时段可预约的场地。

    站点结构为 seatArray[场地][时段]，status: 0=可预约, 1=已选中, -1/-2/-3=不可选。
    这里额外做一次布局自检，万一接口返回 [时段][场地] 也能正确读取。
    """
    result = (
        driver.execute_script(
            """
        const vm = window.__sjtu_vm;
        if (!vm) return {seats: [], layout: 'unknown'};
        const seatArray = vm.seatArray || [];
        const topSite = vm.topSite || [];
        const timeIndex = arguments[0];
        const outerCount = seatArray.length;
        let innerCount = 0;
        for (let i = 0; i < outerCount; i++) {
          const len = (seatArray[i] || []).length;
          if (len > innerCount) innerCount = len;
        }
        const fieldCount = topSite.length;
        let layout = 'field-time';
        if (fieldCount > 0 && outerCount !== fieldCount && innerCount === fieldCount) {
          layout = 'time-field';
        }
        const seats = [];
        for (let f = 0; f < fieldCount; f++) {
          const cell = layout === 'field-time'
            ? (seatArray[f] || [])[timeIndex]
            : (seatArray[timeIndex] || [])[f];
          if (cell && Number(cell.status) === 0) {
            seats.push({
              fieldIndex: f,
              timeIndex: timeIndex,
              layout: layout,
              fieldName: topSite[f] && topSite[f].name,
              fieldId: topSite[f] && topSite[f].fieldId,
              status: cell.status,
              price: cell.price,
              count: cell.count
            });
          }
        }
        return {
          seats: seats,
          layout: layout,
          fieldCount: fieldCount,
          outerCount: outerCount,
          innerCount: innerCount,
          activeType: vm.isactiveName || '',
          date: vm.timeData && vm.timeData.isdate
        };
        """,
            row_index,
        )
        or {}
    )

    seats = result.get("seats") or []
    for seat in seats:
        seat["grid"] = {
            "layout": result.get("layout"),
            "fieldCount": result.get("fieldCount"),
            "outerCount": result.get("outerCount"),
            "innerCount": result.get("innerCount"),
            "date": result.get("date"),
        }
    return seats


def reset_selection(driver: webdriver.Chrome) -> None:
    driver.execute_script("""
        const vm = window.__sjtu_vm;
        if (!vm) return;
        const seatArray = vm.seatArray || [];
        for (let col = 0; col < seatArray.length; col++) {
          for (let row = 0; row < (seatArray[col] || []).length; row++) {
            if (Number(seatArray[col][row].status) === 1) {
              seatArray[col][row].status = 0;
            }
          }
        }
        vm.buyLists = [];
        vm.allSun = 0;
        """)


def choose_seat(driver: webdriver.Chrome, seat: Dict[str, Any]) -> List[Dict[str, Any]]:
    reset_selection(driver)
    grid = seat.get("grid") or {}
    field_index = seat["fieldIndex"]
    time_index = seat["timeIndex"]
    if grid.get("layout") == "time-field":
        outer_index, inner_index = time_index, field_index
    else:
        outer_index, inner_index = field_index, time_index
    buy_lists = driver.execute_script(
        """
        const vm = window.__sjtu_vm;
        vm.handleChooseSeat(arguments[0], arguments[1]);
        return vm.buyLists || [];
        """,
        outer_index,
        inner_index,
    )
    if not buy_lists:
        raise RuntimeError(
            f"选择场地后 buyLists 为空（场地索引={field_index}, 时段索引={time_index}, "
            f"status={seat.get('status')}）"
        )
    return buy_lists


# 页面里可能同时挂载多个验证框（其它触发点的组件一直隐藏），必须只认“真正可见”的那个。
VERIFY_INFO_JS = r"""
const boxes = Array.from(document.querySelectorAll('.verifybox')).filter(function (box) {
  const rect = box.getBoundingClientRect();
  const style = getComputedStyle(box);
  return rect.width > 10 && rect.height > 10 &&
    style.display !== 'none' && style.visibility !== 'hidden';
});

const vm = window.__sjtu_vm;
const ref = vm && vm.$refs && vm.$refs.verify;
const child = ref && ref.$children && ref.$children[0];
const out = {
  found: false,
  kind: '',
  componentType: (ref && ref.componentType) || '',
  childName: (child && child.$options && child.$options.name) || '',
  passFlag: !!(child && child.passFlag),
  tipText: '',
  imgWidth: 0,
  barRect: null,
  handleRect: null,
  panelRect: null,
  bgSrc: '',
  pieceSrc: '',
  bgNatural: null
};
if (!boxes.length) return out;

const box = boxes[0];
out.found = true;
out.kind = box.querySelector('.point-word-tip')
  ? 'clickWord'
  : (box.querySelector('.verify-move-block') ? 'slide' : 'unknown');

const rectOf = function (el) {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
};

const msg = box.querySelector('.verify-msg');
out.tipText = msg ? (msg.innerText || '').trim() : '';
out.barRect = rectOf(box.querySelector('.verify-bar-area'));
out.handleRect = rectOf(box.querySelector('.verify-move-block'));

const panel = box.querySelector('.verify-img-panel');
out.panelRect = rectOf(panel);
const bgImg = panel ? panel.querySelector('img') : null;
const pieceImg = box.querySelector('.verify-sub-block img');
out.bgSrc = bgImg ? (bgImg.getAttribute('src') || '') : '';
out.pieceSrc = pieceImg ? (pieceImg.getAttribute('src') || '') : '';
if (bgImg) out.bgNatural = [bgImg.naturalWidth, bgImg.naturalHeight];

const setSize = child && child.setSize;
out.imgWidth = setSize ? (parseInt(String(setSize.imgWidth).replace('px', ''), 10) || 0) : 0;
return out;
"""

# 站点默认下发 clickWord（点选文字）验证，图片上的汉字无法用当前环境的 OCR 识别；
# 服务端同时支持 blockPuzzle（滑块），把组件的 captchaType 切过去就能用模板匹配自动求解。
VERIFY_FORCE_SLIDE_JS = r"""
const vm = window.__sjtu_vm;
const ref = vm && vm.$refs && vm.$refs.verify;
if (!ref) return {ok: false, reason: '页面上没有验证组件'};
try {
  if (ref.componentType !== 'VerifySlide') {
    ref.captchaType = 'blockPuzzle';
  }
  if (ref.refresh) ref.refresh();
} catch (err) {
  return {ok: false, reason: String(err)};
}
return {ok: true, componentType: ref.componentType};
"""

# 用合成鼠标事件拖动滑块：位移完全可控，且不依赖真实指针，headless 下同样有效。
VERIFY_DRAG_START_JS = r"""
const boxes = Array.from(document.querySelectorAll('.verifybox')).filter(function (box) {
  const rect = box.getBoundingClientRect();
  const style = getComputedStyle(box);
  return rect.width > 10 && rect.height > 10 &&
    style.display !== 'none' && style.visibility !== 'hidden';
});
if (!boxes.length) return {ok: false, reason: '验证框不可见'};
const handle = boxes[0].querySelector('.verify-move-block');
if (!handle) return {ok: false, reason: '未找到滑块手柄'};
const rect = handle.getBoundingClientRect();
window.__sjtu_drag = {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
handle.dispatchEvent(new MouseEvent('mousedown', {
  bubbles: true, cancelable: true, view: window, button: 0, buttons: 1,
  clientX: window.__sjtu_drag.x, clientY: window.__sjtu_drag.y
}));
return {ok: true, x: Math.round(window.__sjtu_drag.x), y: Math.round(window.__sjtu_drag.y)};
"""

VERIFY_DRAG_MOVE_JS = r"""
if (!window.__sjtu_drag) return {ok: false};
window.__sjtu_drag.x += Number(arguments[0]) || 0;
window.dispatchEvent(new MouseEvent('mousemove', {
  bubbles: true, cancelable: true, view: window, button: 0, buttons: 1,
  clientX: window.__sjtu_drag.x, clientY: window.__sjtu_drag.y
}));
return {ok: true, x: Math.round(window.__sjtu_drag.x)};
"""

VERIFY_DRAG_END_JS = r"""
if (!window.__sjtu_drag) return {ok: false};
window.dispatchEvent(new MouseEvent('mouseup', {
  bubbles: true, cancelable: true, view: window, button: 0, buttons: 0,
  clientX: window.__sjtu_drag.x, clientY: window.__sjtu_drag.y
}));
window.__sjtu_drag = null;
return {ok: true};
"""


def _b64_to_image(data: str) -> Optional[Any]:
    """把前端 base64 图片（可带 data: 前缀）解成 OpenCV 图像。"""
    if not data or cv2 is None or np is None:
        return None
    text = data.split(",", 1)[-1] if data.startswith("data:") else data
    try:
        raw = base64.b64decode(text)
    except Exception:
        return None
    if not raw:
        return None
    buf = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_UNCHANGED) if buf.size else None


def _crop_piece(block: Any) -> tuple:
    """裁掉拼图块图片的透明边，返回 (拼图片, alpha, 上边行号, 左边列号)。

    返回的偏移很关键：服务端下发的拼图是 47x155 的整高条带，
    拼图块在条带里的纵向位置就是缺口在背景图里的纵向位置，
    水平位置则要从拖动距离里扣除。
    """
    if block is None:
        return None, None, 0, 0
    if block.ndim == 3 and block.shape[2] == 4:
        alpha = block[:, :, 3]
        ys, xs = np.where(alpha > 8)
        if xs.size:
            top, left = int(ys.min()), int(xs.min())
            rows = slice(top, int(ys.max()) + 1)
            cols = slice(left, int(xs.max()) + 1)
            return block[rows, cols], alpha[rows, cols], top, left
    return block, None, 0, 0


def detect_slider_gap(bg: Any, piece: Any) -> Optional[Dict[str, Any]]:
    """用模板匹配找出拼图缺口在背景图中的位置。

    关键点：拼图块的纵向位置是固定的（条带里块的位置就是缺口位置），
    所以只在它所在的那几行做水平搜索，既快又极准；全图搜索很容易被纹理带偏。

    返回 {gapX, gapY, pieceLeft, score, methods, bgWidth, bgHeight}；
    gapX/gapY 是背景图（服务端原图，通常 310x155）自身像素坐标。
    """
    if bg is None or piece is None or cv2 is None or np is None:
        return None

    bg_h, bg_w = bg.shape[:2]
    patch, alpha, top, left = _crop_piece(piece)
    if patch is None or patch.shape[0] < 8 or patch.shape[1] < 8:
        return None
    ph, pw = patch.shape[:2]
    if ph > bg_h or pw > bg_w:
        return None

    bg_gray = cv2.cvtColor(bg[:, :, :3], cv2.COLOR_BGR2GRAY)
    piece_gray = cv2.cvtColor(patch[:, :, :3], cv2.COLOR_BGR2GRAY)
    piece_edge = cv2.Canny(piece_gray, 60, 160).astype(np.float32)

    candidates: List[Dict[str, Any]] = []
    for dy in (0, 1, -1, 2, -2):
        y = top + dy
        if y < 0 or y + ph > bg_h:
            continue
        band = bg_gray[y : y + ph, :]
        band_edge = cv2.Canny(band, 60, 160).astype(np.float32)

        # 1) 灰度归一化相关：缺口处的拼图内容和拼图块最相似
        try:
            res = cv2.matchTemplate(band, piece_gray, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if np.isfinite(score):
                candidates.append(
                    {
                        "x": int(loc[0]),
                        "y": y,
                        "score": float(score),
                        "method": f"gray{dy:+d}",
                    }
                )
        except cv2.error:
            pass

        # 2) 边缘相关：对缺口被压暗/偏移的图更稳
        try:
            res = cv2.matchTemplate(band_edge, piece_edge, cv2.TM_CCORR_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if np.isfinite(score):
                candidates.append(
                    {
                        "x": int(loc[0]),
                        "y": y,
                        "score": float(score),
                        "method": f"edge{dy:+d}",
                    }
                )
        except cv2.error:
            pass

        # 3) 按拼图块形状加权的方差匹配：只用作投票（不参与分数排名）
        if alpha is not None:
            try:
                mask = (alpha > 8).astype(np.float32)
                res = cv2.matchTemplate(band, piece_gray, cv2.TM_SQDIFF, mask=mask)
                min_val, _, loc, _ = cv2.minMaxLoc(res)
                if np.isfinite(min_val):
                    candidates.append(
                        {
                            "x": int(loc[0]),
                            "y": y,
                            "score": 0.0,
                            "method": f"shape{dy:+d}",
                        }
                    )
            except cv2.error:
                pass

    if not candidates:
        return None

    # 各算法的结论不一定相同：按“横坐标聚簇 + 票数”投票，
    # 票数越多越可信；票数相同时再比分数。
    clusters: List[Dict[str, Any]] = []
    for cand in candidates:
        bucket = None
        for cluster in clusters:
            if abs(cand["x"] - cluster["x_mean"]) <= 3:
                bucket = cluster
                break
        if bucket is None:
            clusters.append({"items": [cand], "x_mean": float(cand["x"])})
        else:
            bucket["items"].append(cand)
            bucket["x_mean"] = sum(c["x"] for c in bucket["items"]) / len(
                bucket["items"]
            )

    clusters.sort(
        key=lambda cluster: (
            len(cluster["items"]),
            sum(c["score"] for c in cluster["items"]),
        ),
        reverse=True,
    )
    best = clusters[0]
    top_score = max(c["score"] for c in best["items"])
    margin = 0.0
    if len(clusters) > 1:
        margin = top_score - max(c["score"] for c in clusters[1]["items"])
    gap_y = next((c["y"] for c in best["items"] if c["method"] == "gray+0"), top)
    return {
        "gapX": int(round(best["x_mean"])),
        "gapY": gap_y,
        "pieceLeft": left,
        "score": top_score,
        "votes": len(best["items"]),
        "margin": margin,
        "methods": [c["method"] for c in best["items"]],
        "bgWidth": bg_w,
        "bgHeight": bg_h,
    }


def human_track(distance: float) -> List[float]:
    """生成拟人拖动轨迹的相对位移（先快后慢 + 微抖动）。"""
    if distance <= 0:
        return [0.0]
    steps = max(16, min(60, int(distance / 3)))
    offsets: List[float] = []
    current = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 2.2
        target = distance * eased
        if i < steps:
            target += random.uniform(-0.6, 0.6)
        offsets.append(target - current)
        current = target
    offsets.append(distance - current)
    return offsets


def get_verify_info(driver: webdriver.Chrome) -> Dict[str, Any]:
    """读取当前“可见”的验证框状态（类型、尺寸、背景图/拼图块素材）。"""
    return _safe_call(lambda: driver.execute_script(VERIFY_INFO_JS), {}) or {}


def _save_debug_image(save_dir: str, name: str, src: str) -> None:
    """把前端 base64 图存盘，便于排查识别效果。"""
    image = _b64_to_image(src or "")
    if image is None or cv2 is None:
        return
    _safe_call(lambda: cv2.imwrite(os.path.join(save_dir, name), image))


def wait_slide_ready(driver: webdriver.Chrome, timeout: float = 15.0) -> Dict[str, Any]:
    """等待滑块素材（背景图 + 拼图块）真正加载完成。"""
    deadline = time.time() + timeout
    latest: Dict[str, Any] = {}
    while time.time() < deadline:
        info = get_verify_info(driver)
        latest = info
        if not info.get("found"):
            return info
        if info.get("kind") == "slide":
            bg = _b64_to_image(info.get("bgSrc") or "")
            piece = _b64_to_image(info.get("pieceSrc") or "")
            if bg is not None and piece is not None and min(piece.shape[:2]) >= 8:
                info["bgImage"] = bg
                info["pieceImage"] = piece
                return info
        time.sleep(0.3)
    return latest


def perform_slider_drag(driver: webdriver.Chrome, distance: float) -> bool:
    """用合成鼠标事件把滑块精确拖动 distance 像素。"""
    started = _safe_call(lambda: driver.execute_script(VERIFY_DRAG_START_JS), {}) or {}
    if not started.get("ok"):
        print(f"[自动验证] 无法开始拖动：{started.get('reason')}")
        return False
    for dx in human_track(distance):
        _safe_call(lambda: driver.execute_script(VERIFY_DRAG_MOVE_JS, dx))
        time.sleep(random.uniform(0.008, 0.03))
    _safe_call(lambda: driver.execute_script(VERIFY_DRAG_END_JS))
    return True


def wait_verify_outcome(
    driver: webdriver.Chrome, old_bg_src: str, timeout: float = 12.0
) -> str:
    """等待验证结果：passed（通过）/ failed（素材已刷新，即失败重来）/ timeout。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = get_verify_info(driver)
        if not info.get("found") or info.get("passFlag"):
            return "passed"
        src = info.get("bgSrc") or ""
        if src and src != old_bg_src:
            return "failed"
        time.sleep(0.3)
    return "timeout"


def solve_verify_captcha(
    driver: webdriver.Chrome,
    save_dir: str = ".",
    max_attempts: int = 5,
) -> bool:
    """自动完成站点的人机验证（AJ-Captcha），成功返回 True。

    站点默认下发 clickWord（点选文字）：图片上的汉字需要中文 OCR 才能定位，
    而服务端同样支持 blockPuzzle（滑块）。因此先把组件切换到滑块，再用
    模板匹配定位缺口，最后用合成鼠标事件精确拖动滑块。
    """
    if cv2 is None or np is None:
        print("[自动验证] 未安装 opencv-python / numpy，无法自动识别，转为手动")
        return False

    info = get_verify_info(driver)
    if not info.get("found"):
        print("[自动验证] 未检测到验证框，按已通过处理")
        return True

    kind = info.get("kind") or "未知"
    tip = info.get("tipText") or ""
    print(f"[自动验证] 验证类型: {kind}{f'（{tip}）' if tip else ''}")

    if info.get("kind") != "slide":
        switched = (
            _safe_call(lambda: driver.execute_script(VERIFY_FORCE_SLIDE_JS), {}) or {}
        )
        if not switched.get("ok"):
            print(f"[自动验证] 切换为滑块验证失败：{switched.get('reason')}")
            return False
        print("[自动验证] 已把点选文字验证切换为滑块验证（blockPuzzle）")
        time.sleep(0.8)

    for attempt in range(1, max_attempts + 1):
        info = wait_slide_ready(driver)
        if not info.get("found"):
            print("[自动验证] 验证框已关闭，视为通过")
            return True
        if info.get("kind") != "slide":
            print(f"[自动验证] 验证类型变为 {info.get('kind') or '未知'}，无法自动完成")
            return False

        bg, piece = info.get("bgImage"), info.get("pieceImage")
        if bg is None or piece is None:
            print(f"[自动验证] 第 {attempt}/{max_attempts} 次：素材未加载，刷新重试")
            _safe_call(lambda: driver.execute_script(VERIFY_FORCE_SLIDE_JS))
            time.sleep(1.2)
            continue

        gap = detect_slider_gap(bg, piece)
        if not gap:
            print(
                f"[自动验证] 第 {attempt}/{max_attempts} 次：拼图素材解析失败，刷新重试"
            )
            _safe_call(lambda: driver.execute_script(VERIFY_FORCE_SLIDE_JS))
            time.sleep(1.2)
            continue

        _save_debug_image(save_dir, "captcha-slider-bg.png", info.get("bgSrc") or "")
        _save_debug_image(
            save_dir, "captcha-slider-block.png", info.get("pieceSrc") or ""
        )

        # 各算法结论打架（裕度为负）说明这张图不好认，直接换一张，
        # 免得白白浪费一次服务端校验。
        if gap["votes"] < 2 or gap["margin"] < 0:
            print(
                f"[自动验证] 第 {attempt}/{max_attempts} 次：识别结果存疑"
                f"（票数={gap['votes']} 裕度={gap['margin']:.2f}），换一张图重试"
            )
            _safe_call(lambda: driver.execute_script(VERIFY_FORCE_SLIDE_JS))
            time.sleep(1.0)
            continue

        panel_width = float((info.get("panelRect") or [0, 0, 0, 0])[2] or 0)
        img_width = (
            float(info.get("imgWidth") or 0) or panel_width or float(gap["bgWidth"])
        )
        scale = (img_width / float(gap["bgWidth"])) if gap["bgWidth"] else 1.0
        # 拼图块在条带里本身有 left 偏移，拖动距离要把它扣掉
        distance = max(6.0, (gap["gapX"] - gap["pieceLeft"]) * scale)
        print(
            f"[自动验证] 第 {attempt}/{max_attempts} 次：缺口 x={gap['gapX']}px "
            f"y={gap['gapY']} 票数={gap['votes']} 分数={gap['score']:.2f} 裕度={gap['margin']:.2f}"
            f"（块偏移={gap['pieceLeft']}），缩放={scale:.3f} → 拖动 {distance:.1f}px"
        )

        if not perform_slider_drag(driver, distance):
            return False

        outcome = wait_verify_outcome(driver, info.get("bgSrc") or "")
        if outcome == "passed":
            print("[自动验证] 滑块验证通过")
            return True
        print(
            f"[自动验证] 第 {attempt}/{max_attempts} 次未通过（{outcome}），刷新后重试"
        )
        time.sleep(1.0)

    print("[自动验证] 多次尝试仍未通过，转为手动完成")
    return False


def submit_order(
    driver: webdriver.Chrome,
    timeout: int = 45,
    headless: bool = False,
    verify_timeout: int = 180,
    auto_verify: bool = True,
    save_dir: str = ".",
) -> SubmitResult:
    """提交订单。

    站点 agreeTerms() 会 POST /venue/personal/ConfirmOrder，然后按返回 code 分支：
      0      -> 写 sessionStorage.newOrderDetailsId 并跳转
      1002/1003 -> 弹出滑块/点选人机验证
      10001  -> 弹“完善个人信息”确认框
      其它   -> $message.error(msg)
    这里直接拦截 XHR/fetch 拿到真实响应，避免只靠 DOM 猜状态而超时。
    """
    driver.execute_script("""
        const vm = window.__sjtu_vm;
        sessionStorage.removeItem('newOrderDetailsId');

        if (!window.__sjtu_net) {
          window.__sjtu_net = [];
          const record = (url, status, text) => {
            let data = text;
            try { data = JSON.parse(text); } catch (e) {}
            window.__sjtu_net.push({url: String(url || ''), status: status, data: data});
            if (window.__sjtu_net.length > 20) window.__sjtu_net.shift();
          };
          const origOpen = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function (m, u) {
            this.__sjtu_url = u;
            return origOpen.apply(this, arguments);
          };
          const origSend = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.send = function () {
            this.addEventListener('load', function () {
              record(this.__sjtu_url, this.status, this.responseText);
            });
            this.addEventListener('error', function () {
              record(this.__sjtu_url, -1, 'network error');
            });
            return origSend.apply(this, arguments);
          };
          if (window.fetch) {
            const origFetch = window.fetch;
            window.fetch = function (input, init) {
              const url = typeof input === 'string' ? input : (input && input.url);
              return origFetch.apply(this, arguments).then(function (resp) {
                resp.clone().text().then(function (t) {
                  record(url, resp.status, t);
                }).catch(function () {});
                return resp;
              }).catch(function (err) {
                record(url, -1, String(err));
                throw err;
              });
            };
          }
        }

        vm.ischecked = true;
        vm.disabled = false;
        vm.agreeTerms();
        """)

    start = time.time()
    deadline = start + timeout
    last_message = ""
    last_net: Dict[str, Any] = {}
    captcha_prompted = False
    auto_verify_tried = False
    captcha_cleared_at: Optional[float] = None
    resubmits = 0
    last_notice = start
    while time.time() < deadline:
        state = driver.execute_script("""
            const vm = window.__sjtu_vm || {};
            const isVisible = (el) => {
              if (!el) return false;
              const style = getComputedStyle(el);
              return el.getClientRects().length > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
            };
            const messages = Array.from(
              document.querySelectorAll('.el-message__content, .el-form-item__error')
            ).map((x) => (x.innerText || '').trim()).filter(Boolean);
            const confirmBox = document.querySelector('.el-message-box');
            const all = window.__sjtu_net || [];
            const net = all.filter((x) => x && /ConfirmOrder/i.test(String(x.url || '')));
            // 页面里可能同时挂载多个验证框（其它触发点的组件一直隐藏），只认真正可见的那个
            const bodyText = (document.body && document.body.innerText) || '';
            const visibleVerifies = Array.from(document.querySelectorAll('.verifybox')).filter((box) => {
              const rect = box.getBoundingClientRect();
              const style = getComputedStyle(box);
              return rect.width > 10 && rect.height > 10 &&
                style.display !== 'none' && style.visibility !== 'hidden';
            });
            const captchaBox = visibleVerifies.length ? visibleVerifies[0] : null;
            return {
              href: window.location.href,
              orderId: sessionStorage.getItem('newOrderDetailsId') || '',
              captcha: !!captchaBox,
              captchaKind: captchaBox
                ? (captchaBox.querySelector('.point-word-tip')
                    ? 'clickWord'
                    : (captchaBox.querySelector('.verify-move-block') ? 'slide' : 'unknown'))
                : '',
              confirmText: isVisible(confirmBox)
                ? (confirmBox.innerText || '').trim().slice(0, 300)
                : '',
              disabled: !!vm.disabled,
              messages: messages,
              netTotal: all.length,
              recentUrls: all.slice(-3).map((x) => String((x && x.url) || '')),
              recentAll: all.slice(-6).map((x) => ({
                url: String((x && x.url) || ''),
                status: x && x.status,
                body: String(
                  x && (typeof x.data === 'string' ? x.data : JSON.stringify(x.data || ''))
                ).slice(0, 400)
              })),
              net: net.length ? net[net.length - 1] : null
            };
            """)

        href = state.get("href", "")
        net = state.get("net") or {}
        payload = net.get("data") if isinstance(net.get("data"), dict) else {}
        try:
            code = int(payload.get("code")) if payload.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        msg = str(payload.get("msg") or "").strip()
        if net:
            last_net = {"code": code, "msg": msg, "http": net.get("status")}

        order_id = state.get("orderId", "")
        if order_id or "/orderDetails/" in href or "/paymentResult/" in href:
            return SubmitResult("success", "订单提交成功", order_id)
        # 接口已返回 code=0，路由跳转可能稍有延迟，直接判定成功
        if code == 0:
            return SubmitResult(
                "success", "订单提交成功", order_id or str(payload.get("data") or "")
            )
        if code in (1002, 1003) or state.get("captcha"):
            reason = f"code={code if code is not None else '?'}"
            captcha_visible = bool(state.get("captcha"))
            if auto_verify and not auto_verify_tried:
                auto_verify_tried = True
                print(f"[提交诊断] ConfirmOrder 响应: {last_net or '（未捕获）'}")
                print(
                    f"[自动验证] 检测到人机验证（{reason}，"
                    f"类型={state.get('captchaKind') or '未知'}），尝试自动完成…"
                )
                focus_browser(driver)
                if solve_verify_captcha(driver, save_dir=save_dir):
                    print("[自动验证] 已通过，等待订单结果…")
                    time.sleep(1.0)
                    deadline = max(deadline, time.time() + 60)
                    continue
                if headless:
                    return SubmitResult(
                        "captcha",
                        f"服务器要求人机验证（{reason}），headless 下自动完成失败；"
                        "请去掉 --headless 后重试（脚本会停下来等你手动验证）",
                    )
                print("[自动验证] 未能自动通过，转为手动完成")
            elif not auto_verify and not captcha_prompted:
                print(
                    "[提示] 已指定 --no-auto-verify，不会自动拖动滑块；"
                    "去掉该参数即可让脚本自行尝试"
                )
                if headless:
                    return SubmitResult(
                        "captcha",
                        f"服务器要求人机验证（{reason}），已禁用自动验证且 headless 无法手动完成",
                    )

            if not captcha_prompted:
                print(f"[提交诊断] ConfirmOrder 响应: {last_net or '（未捕获）'}")
                print(
                    "=" * 60 + "\n"
                    f"[人机验证] 服务器要求滑块/点选验证（{reason}）。\n"
                    "[人机验证] 请在浏览器窗口中手动完成验证，完成后脚本会自动继续。\n"
                    f"[人机验证] 最长等待 {verify_timeout} 秒，请不要关闭浏览器窗口。\n"
                    + "=" * 60
                )
                focus_browser(driver)
                captcha_prompted = True
                deadline = max(deadline, time.time() + verify_timeout)
            elif not captcha_visible:
                # 验证框已关闭但服务端仍未确认：可能是前端没有自动重发请求
                if captcha_cleared_at is None:
                    captcha_cleared_at = time.time()
                elif time.time() - captcha_cleared_at > 8 and resubmits < 2:
                    resubmits += 1
                    captcha_cleared_at = None
                    print(
                        f"[人机验证] 验证框已关闭但订单仍未提交，"
                        f"自动重新提交（第 {resubmits} 次）"
                    )
                    _safe_call(
                        lambda: driver.execute_script(
                            "const vm = window.__sjtu_vm; if (!vm) return;"
                            "vm.ischecked = true; vm.disabled = false; vm.agreeTerms();"
                        )
                    )
            else:
                captcha_cleared_at = None

            if captcha_prompted and time.time() - last_notice > 15:
                last_notice = time.time()
                left = int(deadline - time.time())
                print(f"[人机验证] 仍在等待你完成验证（剩余约 {max(0, left)} 秒）…")
            time.sleep(0.5)
            continue
        if code == 10001:
            return SubmitResult(
                "failed", f"需要先完善个人信息：{msg or '请到“我的信息”补全后再试'}"
            )
        if code is not None and code != 0:
            return SubmitResult("failed", f"接口返回 code={code}: {msg or '未知错误'}")
        if net and net.get("status") not in (None, 200):
            return SubmitResult(
                "failed", f"接口请求失败: HTTP {net.get('status')} {msg}".strip()
            )
        if state.get("confirmText"):
            return SubmitResult("failed", f"需要人工确认的弹窗：{state['confirmText']}")
        if state.get("messages"):
            last_message = "；".join(state["messages"])

        # 一直没有看到 ConfirmOrder 请求，说明提交动作根本没发出去
        if not net and time.time() - start > 5 and not state.get("netTotal"):
            return SubmitResult(
                "failed",
                "提交订单请求未发出（页面状态异常：可能 Vue 实例失效或前置校验未通过）",
            )
        time.sleep(0.5)

    if last_net:
        detail = f"（最后响应: {last_net}）"
    elif state and state.get("netTotal"):
        detail = f"（未匹配到 ConfirmOrder 请求，最近请求: {state.get('recentUrls')}）"
    else:
        detail = "（未捕获到任何请求，可能是网络/风控拦截）"
    print("[提交诊断] 最后一次页面状态:")
    print(f"  URL={state.get('href')}")
    print(
        f"  orderId={state.get('orderId')!r} disabled={state.get('disabled')} "
        f"messages={state.get('messages')} confirmText={state.get('confirmText')!r}"
    )
    for item in state.get("recentAll") or []:
        print(
            f"  请求 {item.get('url')} -> HTTP {item.get('status')} {item.get('body')}"
        )
    if not (state.get("recentAll") or []):
        print("  （未捕获任何 XHR/fetch 请求）")
    return SubmitResult("failed", (last_message or "提交订单超时") + detail)


def book(
    driver: webdriver.Chrome,
    venue: str,
    item_name: str,
    period: int,
    target_date: Optional[str] = None,
    headless: bool = False,
    verify_timeout: int = 180,
    auto_verify: bool = True,
    save_dir: str = ".",
) -> bool:
    venue_id = TARGET_VENUE_IDS.get(venue)
    if not venue_id:
        raise RuntimeError(f"未知场馆 {venue!r}，请在 TARGET_VENUE_IDS 中补充 venueId")

    row_index = period - 1
    if row_index < 0 or row_index >= len(TIME_SLOTS):
        raise RuntimeError(f"period 必须在 1-{len(TIME_SLOTS)} 之间")

    target_slot = TIME_SLOTS[row_index]
    url = booking_url(venue_id, item_name)
    print(f"打开预约详情页: {url}")
    driver.get(url)

    snapshot = wait_for_details_ready(driver)
    active_item = select_motion_type(driver, item_name, venue=venue)
    print(f"已进入 {snapshot.get('venueName')}，当前项目 {active_item}")

    week_list = get_week_list(driver)
    single_date = bool(target_date)
    if single_date:
        target_ymd = extract_date_parts(target_date)
        if not target_ymd:
            raise RuntimeError(f"无法解析指定日期: {target_date!r}")
        match_index = next(
            (
                idx
                for idx, item in enumerate(week_list)
                if date_matches(item, target_ymd)
            ),
            None,
        )
        if match_index is None:
            options = (
                "；".join(
                    f"{item.get('view')}({item.get('date')})" for item in week_list
                )
                or "（无）"
            )
            raise RuntimeError(
                f"指定日期 {target_date} 不在可预约范围；当前可选日期: {options}"
            )
        date_indices = [match_index]
        chosen = week_list[match_index]
        print(
            f"仅处理指定日期: {chosen.get('view')} {chosen.get('date')}"
            f"（第 {match_index + 1}/{len(week_list)} 个可预约日期）"
        )
    else:
        date_indices = list(range(len(week_list) - 1, -1, -1))
        print(f"未指定日期，按原逻辑从最远日期往前遍历 {len(week_list)} 天")

    success_count = 0
    for date_index in date_indices:
        try:
            date_info = select_date(driver, date_index)
            seats = available_seats(driver, row_index)
        except Exception as exc:
            suffix = "" if single_date else "，切换到前一天"
            print(f"日期索引 {date_index} 加载失败{suffix}: {exc}")
            continue

        if not seats:
            print(f"{date_info['view']} {target_slot} 无可用场地")
            grid = describe_seat_grid(driver, row_index)
            print(
                f"[诊断] 项目={grid.get('activeType')} 场地数={grid.get('fieldCount')} "
                f"seatArray={grid.get('outerCount')}x{grid.get('innerCount')} "
                f"紧张度={grid.get('nerData')} 状态分布={grid.get('statuses')}"
            )
            print(
                f"[诊断] 日期={grid.get('timeData')} 场地={grid.get('fields')} "
                f"目标行({target_slot})状态={grid.get('rowStatus')}"
            )
            continue

        seat = seats[0]
        print(
            f"{date_info['view']} {target_slot} 找到 {len(seats)} 个可用场地，"
            f"尝试 {seat.get('fieldName')}"
        )

        try:
            buy_lists = choose_seat(driver, seat)
            print(
                "已选择: "
                + ", ".join(
                    f"{x.get('subSitename')} {x.get('scheduleTime')}" for x in buy_lists
                )
            )
            result = submit_order(
                driver,
                headless=headless,
                verify_timeout=verify_timeout,
                auto_verify=auto_verify,
                save_dir=save_dir,
            )
            print(f"提交结果: {result.status} {result.message}")
            if result.status == "success":
                success_count += 1
                if single_date:
                    print(f"{date_info['view']} 预约成功")
                    break
                print(f"{date_info['view']} 预约成功，继续遍历前一天")
                driver.get(url)
                wait_for_details_ready(driver)
                select_motion_type(driver, item_name, venue=venue)
                continue
            if result.status == "captcha":
                print(f"人机验证未通过，结束本轮遍历：{result.message}")
                return success_count > 0
            suffix = "" if single_date else "，切换到前一天"
            print(f"{date_info['view']} 提交未成功{suffix}")
        except Exception as exc:
            suffix = "" if single_date else "，切换到前一天"
            print(
                f"{date_info['view']} 场地 {seat.get('fieldName')} 尝试失败{suffix}: {exc}"
            )
        finally:
            reset_selection(driver)

    if success_count:
        print(f"本轮完成，成功预约 {success_count} 天")
        return True

    if single_date:
        print(f"指定日期 {target_date} 的 {target_slot} 未预约成功")
    else:
        print(f"本轮遍历完成，没有成功预约任一天 {target_slot}")
    return False


# ---------------------------------------------------------------- 仅监控模式


def query_slot_availability(
    driver: webdriver.Chrome,
    venue: str,
    item_name: str,
    period: int,
    target_date: str,
) -> Dict[str, Any]:
    """只读地查询指定日期/时段是否还有可预约场地（不选座、不下单）。

    返回 status：
      - available        有可预约场地
      - full             该时段已约满/无可用场地
      - date-unavailable 目标日期还没放开或已过期
    """
    venue_id = TARGET_VENUE_IDS.get(venue)
    if not venue_id:
        raise RuntimeError(f"未知场馆 {venue!r}，请在 TARGET_VENUE_IDS 中补充 venueId")

    row_index = period - 1
    if row_index < 0 or row_index >= len(TIME_SLOTS):
        raise RuntimeError(f"period 必须在 1-{len(TIME_SLOTS)} 之间")
    target_slot = TIME_SLOTS[row_index]

    target_ymd = extract_date_parts(target_date)
    if not target_ymd:
        raise RuntimeError(f"无法解析日期: {target_date!r}")

    driver.get(booking_url(venue_id, item_name))
    wait_for_details_ready(driver)
    select_motion_type(driver, item_name, venue=venue)

    week_list = get_week_list(driver)
    match_index = next(
        (idx for idx, item in enumerate(week_list) if date_matches(item, target_ymd)),
        None,
    )
    if match_index is None:
        return {
            "status": "date-unavailable",
            "slot": target_slot,
            "available_dates": [
                f"{item.get('view')}({item.get('date')})" for item in week_list
            ],
        }

    date_info = select_date(driver, match_index)
    seats = available_seats(driver, row_index)
    result: Dict[str, Any] = {
        "status": "available" if seats else "full",
        "slot": target_slot,
        "date": date_info,
        "seats": seats,
    }
    if not seats:
        result["grid"] = describe_seat_grid(driver, row_index)
    return result


def build_monitor_mail(
    venue: str,
    item_name: str,
    target_date: str,
    row_index: int,
    seats: List[Dict[str, Any]],
    date_info: Optional[Dict[str, Any]],
) -> tuple:
    """拼出通知邮件的标题与正文。"""
    slot = TIME_SLOTS[row_index]
    view = (date_info or {}).get("view") or ""
    names = [
        str(seat.get("fieldName") or f"场地{i + 1}") for i, seat in enumerate(seats)
    ]
    subject = f"[SJTU场馆] {venue} {item_name} {target_date} {slot} 有可预约场地"

    venue_id = TARGET_VENUE_IDS.get(venue)
    link = booking_url(venue_id, item_name) if venue_id else BASE_URL
    body_lines = [
        "上海交通大学体育场馆预约提醒",
        "",
        f"场馆：{venue}",
        f"项目：{item_name}",
        f"日期：{target_date}{f'（{view}）' if view else ''}",
        f"时段：{slot}",
        f"可预约场地：{', '.join(names) if names else '（未识别）'}",
        f"检查时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "预约详情页：",
        link,
        "",
        "说明：本条为只读监控通知，脚本没有替你下单，请尽快自行预约。",
    ]
    return subject, "\n".join(body_lines)


def send_notification_email(subject: str, body: str, args: argparse.Namespace) -> bool:
    """通过 SMTP 发送通知邮件；未配置账号时只提示，不抛异常。"""
    to_addr = (args.mail_to or "").strip()
    user = (args.smtp_user or "").strip()
    password = args.smtp_password or ""

    if not to_addr:
        print("[监控] 未指定收件人（--mail-to），跳过邮件通知")
        return False
    if not user or not password:
        print("[监控] 未配置 SMTP 账号，跳过邮件通知（可约信息已打印到控制台）")
        return False

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = user
    message["To"] = to_addr
    message["Date"] = formatdate(localtime=True)

    host = args.smtp_host
    port = int(args.smtp_port)
    recipients = [addr.strip() for addr in to_addr.split(",") if addr.strip()]
    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=25)
        else:
            server = smtplib.SMTP(host, port, timeout=25)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(user, password)
        server.sendmail(user, recipients, message.as_string())
    except Exception as exc:
        print(f"[监控] 邮件发送失败: {type(exc).__name__}: {exc}")
        return False
    finally:
        if server is not None:
            _safe_call(server.quit)

    print(f"[监控] 通知邮件已发送 -> {to_addr}")
    return True


def open_logged_in_driver(args: argparse.Namespace) -> Optional[webdriver.Chrome]:
    """启动浏览器并登录；失败返回 None。"""
    driver = init_driver(
        args.chromedriver,
        headless=args.headless,
        user_data_dir=args.user_data_dir,
        chrome_logs=args.chrome_logs,
    )
    try:
        driver.get(BASE_URL)
        if not login(
            driver,
            args.user,
            args.password,
            save_dir=args.captcha_dir,
            max_attempts=args.max_login_attempts,
            manual_login=args.manual_login,
            login_timeout=args.login_timeout,
            headless=args.headless,
            manual_fallback=args.manual_fallback,
        ):
            _safe_call(driver.quit)
            return None
    except Exception as exc:
        print(f"[监控] 启动浏览器/登录异常: {type(exc).__name__}: {exc}")
        _safe_call(driver.quit)
        return None
    return driver


def run_monitor(args: argparse.Namespace) -> int:
    """仅监控模式：定时只读检查可预约性，可约时发邮件通知。"""
    if not args.date:
        print("[监控] 必须用 --date 指定要监控的日期，例如 --date 2026-11-30")
        return 2
    if not TARGET_VENUE_IDS.get(args.venue):
        print(f"[监控] 未知场馆 {args.venue!r}")
        return 2

    row_index = args.period - 1
    if row_index < 0 or row_index >= len(TIME_SLOTS):
        print(f"[监控] --period 必须在 1-{len(TIME_SLOTS)} 之间")
        return 2

    target_slot = TIME_SLOTS[row_index]
    interval = max(10, int(args.monitor_interval))
    mail_ready = bool(args.smtp_user and args.smtp_password)

    print("=" * 64)
    print("[监控] 仅监控模式：只读检查，不会选座、不会下单")
    print(f"[监控] 目标：{args.venue} / {args.item_name} / {args.date} {target_slot}")
    print(f"[监控] 频率：每 {interval} 秒检查一次")
    if mail_ready:
        print(f"[监控] 可约时邮件通知 -> {args.mail_to}")
    else:
        print(
            f"[监控] 可约时打印到控制台（未配置 SMTP 账号，无法发邮件至 {args.mail_to}）"
        )
        print("       配置方式：设置环境变量 SPORTS_SMTP_USER / SPORTS_SMTP_PASSWORD")
        print(
            "       Gmail 需使用「应用专用密码」；也可传 --smtp-user / --smtp-password"
        )
    if args.monitor_repeat:
        print("[监控] 通知后继续监控（--monitor-repeat）")
    else:
        print("[监控] 通知一次后自动退出（如需持续监控请加 --monitor-repeat）")
    print("=" * 64)

    driver: Optional[webdriver.Chrome] = None
    failures = 0
    delivered = False
    try:
        print("[监控] 正在启动浏览器并登录…")
        driver = open_logged_in_driver(args)
        if driver is None:
            print("[监控] 登录失败，无法开始监控")
            return 1

        while True:
            started = time.time()
            stamp = datetime.now()
            result: Dict[str, Any] = {"status": "error"}
            try:
                result = query_slot_availability(
                    driver, args.venue, args.item_name, args.period, args.date
                )
                failures = 0
            except Exception as exc:
                failures += 1
                print(
                    f"[监控 {stamp:%H:%M:%S}] 检查失败（连续 {failures} 次）："
                    f"{type(exc).__name__}: {exc}"
                )

            status = result.get("status")
            if status == "available":
                seats = result.get("seats") or []
                names = [
                    str(seat.get("fieldName") or f"场地{i + 1}")
                    for i, seat in enumerate(seats)
                ]
                print(
                    f"[监控 {stamp:%H:%M:%S}] ★ 发现可预约场地！"
                    f"{args.date} {target_slot} -> {', '.join(names)}"
                )
                subject, body = build_monitor_mail(
                    args.venue,
                    args.item_name,
                    args.date,
                    row_index,
                    seats,
                    result.get("date"),
                )
                print("-" * 64)
                print(body)
                print("-" * 64)
                if send_notification_email(subject, body, args):
                    delivered = True
                if not args.monitor_repeat:
                    print("[监控] 已完成通知，退出监控")
                    break
            elif status == "full":
                grid = result.get("grid") or {}
                print(
                    f"[监控 {stamp:%H:%M:%S}] 暂不可约：{args.date} {target_slot}"
                    f"（场地数={grid.get('fieldCount')} "
                    f"状态分布={grid.get('statuses')}）"
                )
            elif status == "date-unavailable":
                dates = "；".join(result.get("available_dates") or []) or "（无）"
                print(
                    f"[监控 {stamp:%H:%M:%S}] {args.date} 暂不在可预约范围，"
                    f"当前可选日期：{dates}"
                )
            elif failures >= 3:
                print("[监控] 连续失败，重启浏览器并重新登录…")
                if driver is not None:
                    _safe_call(driver.quit)
                    driver = None
                driver = open_logged_in_driver(args)
                failures = 0
                if driver is None:
                    print(f"[监控] 重新登录失败，{interval} 秒后重试")
                    time.sleep(interval)
                    continue

            elapsed = time.time() - started
            wait_seconds = max(5.0, interval - elapsed)
            print(f"[监控 {stamp:%H:%M:%S}] {int(wait_seconds)} 秒后再次检查")
            time.sleep(wait_seconds)

        return 0
    except KeyboardInterrupt:
        print("\n[监控] 收到中断信号，退出监控")
        return 0
    finally:
        if driver is not None:
            _safe_call(driver.quit)
        if delivered:
            print("[监控] 本次监控已成功发出通知")


def run_attempt(args: argparse.Namespace) -> bool:
    driver = None
    try:
        print(
            f"[环境] headless={args.headless} chromedriver="
            f"{args.chromedriver or 'Selenium Manager 自动匹配'} "
            f"user_data_dir={args.user_data_dir or '无'}"
        )
        driver = init_driver(
            args.chromedriver,
            headless=args.headless,
            user_data_dir=args.user_data_dir,
            chrome_logs=args.chrome_logs,
        )
        caps = getattr(driver, "capabilities", {}) or {}
        driver_version = str(caps.get("chromedriverVersion", "")).split(" ")[0]
        print(
            f"[环境] 浏览器已启动: Chrome {caps.get('browserVersion', '?')} "
            f"/ chromedriver {driver_version or '?'}"
        )
        driver.get(BASE_URL)
        if not login(
            driver,
            args.user,
            args.password,
            save_dir=args.captcha_dir,
            max_attempts=args.max_login_attempts,
            manual_login=args.manual_login,
            login_timeout=args.login_timeout,
            headless=args.headless,
            manual_fallback=args.manual_fallback,
        ):
            print("登录失败，本次尝试结束")
            return False
        return book(
            driver,
            venue=args.venue,
            item_name=args.item_name,
            period=args.period,
            target_date=args.date,
            headless=args.headless,
            verify_timeout=args.verify_timeout,
            auto_verify=args.auto_verify,
            save_dir=args.captcha_dir,
        )
    except Exception as exc:
        print(f"预约尝试发生异常: {type(exc).__name__}: {exc}")
        print(traceback.format_exc())
        if driver is not None:
            log_page_diagnostics(
                driver, "attempt-error", args.captcha_dir, with_artifacts=True
            )
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def parse_clock(value: str) -> dt_time:
    try:
        hour, minute = value.split(":", 1)
        return dt_time(int(hour), int(minute))
    except Exception as exc:
        raise argparse.ArgumentTypeError("时间格式应为 HH:MM") from exc


def next_window_start(now: datetime, start: dt_time) -> datetime:
    today = datetime(now.year, now.month, now.day, start.hour, start.minute)
    if now < today:
        return today
    return today + timedelta(days=1)


def run_with_schedule(args: argparse.Namespace) -> int:
    window_start = args.start_time
    window_minutes = args.window_minutes

    while True:
        now = datetime.now()
        start_dt = datetime(
            now.year, now.month, now.day, window_start.hour, window_start.minute
        )
        end_dt = start_dt + timedelta(minutes=window_minutes)

        if not (start_dt <= now < end_dt):
            next_start = next_window_start(now, window_start)
            seconds_until_start = max(1, int((next_start - now).total_seconds()))
            sleep_seconds = max(1, seconds_until_start // 2)
            next_end = next_start + timedelta(minutes=window_minutes)
            print(
                f"[{now:%Y-%m-%d %H:%M:%S}] 当前不在 "
                f"{next_start.strftime('%H:%M')}-{next_end.strftime('%H:%M')} 窗口，"
                f"距下次开始还有 {seconds_until_start} 秒，"
                f"等待 {sleep_seconds} 秒后再次检查"
            )
            time.sleep(sleep_seconds)
            continue

        print(
            f"[{now:%Y-%m-%d %H:%M:%S}] 进入预约窗口，"
            f"每 {args.retry_interval} 秒尝试一次"
        )
        while datetime.now() < end_dt:
            print("-" * 50)
            success = run_attempt(args)
            remaining = (end_dt - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            sleep_seconds = min(args.retry_interval, remaining)
            status = "有成功预约" if success else "未成功预约"
            print(f"本轮遍历完成（{status}），{int(sleep_seconds)} 秒后继续遍历")
            time.sleep(sleep_seconds)

        print("已超出本日预约窗口，等待下一天")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--chromedriver",
        default=None,
        help="chromedriver 可执行文件路径；默认使用 Selenium Manager 自动匹配",
    )
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--venue", default="学生中心")
    parser.add_argument(
        "--item-name",
        default="学生中心健身房",
        help="运动项目名（Tab 名），如 健身房；允许带场馆前缀，会自动模糊匹配",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=9,
        help="预约时间段，7:00-8:00 算 1，15:00-16:00 算 9",
    )
    parser.add_argument(
        "--date",
        type=parse_date_arg,
        default=None,
        help="只预约指定日期，如 2026-11-28 / 11-28 / today / tomorrow；不传则遍历全部可预约日期",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--manual-login", action="store_true", help="手动完成 JAccount 登录"
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=120,
        help="人工登录/人工输入验证码时等待你操作的秒数（默认 120）",
    )
    parser.add_argument(
        "--max-login-attempts",
        type=int,
        default=5,
        help="验证码自动识别最多尝试次数；每次失败都会刷新登录页换新验证码，用尽后转人工输入",
    )
    parser.add_argument(
        "--manual-fallback",
        dest="manual_fallback",
        action="store_true",
        help="验证码自动识别连续失败后转人工输入（默认已开启）",
    )
    parser.add_argument(
        "--no-manual-fallback",
        dest="manual_fallback",
        action="store_false",
        help="验证码自动识别失败后直接放弃，不等待人工输入",
    )
    parser.set_defaults(manual_fallback=True)
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=180,
        help="自动验证失败后，等待你手动完成人机验证的秒数（headless 下无法手动，仅自动验证生效）",
    )
    parser.add_argument(
        "--auto-verify",
        dest="auto_verify",
        action="store_true",
        help="自动完成人机验证（默认已开启：点选文字会先切换为滑块，再用 OpenCV 定位缺口）",
    )
    parser.add_argument(
        "--no-auto-verify",
        dest="auto_verify",
        action="store_false",
        help="关闭自动验证，全部由你手动完成",
    )
    parser.set_defaults(auto_verify=True)
    parser.add_argument("--captcha-dir", default=".")
    parser.add_argument("--user-data-dir", default=None)
    parser.add_argument(
        "--chrome-logs",
        action="store_true",
        help="显示 Chrome/chromedriver 的原生日志（默认静音，仅排查问题时使用）",
    )
    parser.add_argument(
        "--test", action="store_true", help="测试模式：立即运行一次，不等待 12:00"
    )
    parser.add_argument(
        "--only-moniter",
        "--only-monitor",
        dest="only_monitor",
        action="store_true",
        help="仅监控模式：登录后定时只读检查目标日期/时段是否可预约，可约时发邮件通知（不会下单）",
    )
    parser.add_argument(
        "--monitor-interval",
        type=int,
        default=60,
        help="仅监控模式的检查间隔（秒），默认 60",
    )
    parser.add_argument(
        "--monitor-repeat",
        action="store_true",
        help="仅监控模式下发现可预约后继续监控（默认通知一次即退出）",
    )
    parser.add_argument(
        "--mail-to",
        default=DEFAULT_MAIL_TO,
        help=f"通知邮件收件人，多个用逗号分隔（默认 {DEFAULT_MAIL_TO}）",
    )
    parser.add_argument(
        "--smtp-host",
        default=os.environ.get("SPORTS_SMTP_HOST", "smtp.gmail.com"),
        help="SMTP 服务器（默认 smtp.gmail.com，也可用环境变量 SPORTS_SMTP_HOST）",
    )
    parser.add_argument(
        "--smtp-port",
        type=int,
        default=int(os.environ.get("SPORTS_SMTP_PORT", "465")),
        help="SMTP 端口：465 走 SSL，587 走 STARTTLS（默认 465）",
    )
    parser.add_argument(
        "--smtp-user",
        default=os.environ.get("SPORTS_SMTP_USER", ""),
        help="SMTP 账号（同时作为发件人），默认读环境变量 SPORTS_SMTP_USER",
    )
    parser.add_argument(
        "--smtp-password",
        default=os.environ.get("SPORTS_SMTP_PASSWORD", ""),
        help="SMTP 密码/应用专用密码，默认读环境变量 SPORTS_SMTP_PASSWORD",
    )
    parser.add_argument("--start-time", type=parse_clock, default=dt_time(12, 0))
    parser.add_argument("--window-minutes", type=int, default=10)
    parser.add_argument("--retry-interval", type=int, default=15)
    args = parser.parse_args()

    if args.manual_login and args.headless:
        print("--manual-login 需要可见浏览器，请不要同时使用 --headless")
        return 2

    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        print(
            f"警告: 未检测到 tesseract，验证码 OCR 将失败: {type(exc).__name__}: {exc}"
        )
        print("      可执行: sudo apt install tesseract-ocr")

    if args.auto_verify:
        if cv2 is None or np is None:
            print(
                "警告: 未安装 opencv-python / numpy，无法自动完成滑块验证，"
                "遇人机验证时将退化为手动"
            )
            print("      可执行: pip install opencv-python numpy")
        else:
            print(
                f"[环境] 自动人机验证已启用（opencv {cv2.__version__}）："
                "点选文字→必要时先切换为滑块，再用模板匹配定位缺口"
            )
    else:
        print("[环境] 已关闭自动人机验证（--no-auto-verify），全部手动完成")

    if args.chromedriver:
        chromedriver_path = os.path.abspath(args.chromedriver)
        if not Path(chromedriver_path).exists():
            print("找不到 chromedriver，可执行文件路径:", chromedriver_path)
            return 1
        args.chromedriver = chromedriver_path

    Path(args.captcha_dir).mkdir(parents=True, exist_ok=True)

    if args.only_monitor:
        return run_monitor(args)
    if args.test:
        return 0 if run_attempt(args) else 1
    return run_with_schedule(args)


if __name__ == "__main__":
    sys.exit(main())
