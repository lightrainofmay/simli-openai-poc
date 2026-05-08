"""
云南互联网医院 - 号源监控 + 自动抢号脚本（康晓敏医生 / 证据采集版）

新增功能：
1) 数据记录：每次查询结果写入 CSV（schedule_log.csv）
2) 激活窗口：检测到出号后，自动切换高频采样（active window）
3) 事件日志：出号、归零、变动写入 events.csv
4) 自动抢号：检测到可约号后，立刻调用抢号接口（book_attempts.csv 记录结果）

使用：
    pip install requests
    python clinic_monitor.py
"""

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import platform
import smtplib
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

import requests

# ============== 配置区 ==============
# 多医生：改 active_doctor 即可切换监控目标（用于无号时换另一位验证占号链路）
CONFIG = {
    "active_doctor": "kang_xiaomin",  # "kang_xiaomin" | "xiong_yongying"
    "doctors": {
        "kang_xiaomin": {
            "doctor_name": "康晓敏",
            "hospital_id": 49,
            "hospital_area_id": 986,
            "hospital_code": "871102",
            "department_id": 263631,
            "department_code": "1289",
            "doctor_id": 209021,
            "doctor_code": "931322",
        },
        # 来自你 HAR 中预约请求体（儿童养育咨询门诊-熊永英），仅用于测试占号/排班接口是否正常
        "xiong_yongying": {
            "doctor_name": "熊永英",
            "hospital_id": 49,
            "hospital_area_id": 986,
            "hospital_code": "871102",
            "department_id": 263585,
            "department_code": "0944",
            "doctor_id": 208514,
            "doctor_code": "302459",
        },
    },
    # 采样策略
    "normal_interval_seconds": 60,
    "active_interval_seconds": 0.5,
    "active_window_seconds": 300,
    "min_remaining": 1,
    "notify_cooldown_seconds": 1800,
    # 数据文件
    "csv_schedule_log": "schedule_log.csv",
    "csv_events": "events.csv",
    "csv_book_attempts": "book_attempts.csv",
    # 自动抢号配置：请按抓包结果补齐
    "auto_book": {
        "enabled": True,
        # 预约占号接口（不含缴费）
        "api_url": "https://appv3-api.ynhdkc.com/apis/v1/appointment-order/customer/appointment-orders",
        "timeout_seconds": 8,
        # 若同一 schedule 抢号失败，最短重试间隔（避免狂轰接口）
        "retry_cooldown_seconds": 0.2,
        # RELEASE 后短窗口内的突发补抢（总尝试次数 = 1 + burst_retry_count）
        "burst_retry_count": 2,
        "burst_retry_interval_seconds": 0.3,
        "burst_window_seconds": 2.0,
        # 双通道并发：同一号位每次尝试并发发起 N 条请求（建议 2）
        "parallel_channels": 2,
        "parallel_stagger_seconds": 0.08,
        # 鉴权/会话信息（硬编码版，按你抓包值直接填写）
        "headers": {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://appv3.ynhdkc.com",
            "Referer": "https://appv3.ynhdkc.com/",
            "Authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6ImY4NWZhOTNmLTg4NjItNGFhMS04YjM4LTJkOGJmOWEwNDMyYiJ9.eyJpc3MiOiJ0ZXN0IiwiaWF0IjoxNzc3ODg0NjA2LCJleHAiOjE3Nzg0ODk0MDYsImNoYW5uZWwiOiJDTElFTlRfVVNFUiIsInNjb3BlIjpbInVzZXIiXSwiYXV0aCI6MCwidXNlciI6MTg5ODk0LCJwaG9uZV9udW1iZXIiOiIxMzcqKioqOTY4OSIsInVzZXJfaWQiOjE4OTg5NCwiZHpqX3N1YnNjcmliZSI6MCwibmV3X3N1YnNjcmliZSI6MSwibmV3X29wZW5pZCI6Im83TENYNkx0OW5CWXVpbjVxQ1pMdGdlbTVyNzgiLCJkempfb3BlbmlkIjoiIiwidW5pb25faWQiOiJvTlF6NDBXbVl4cER5d0t3WGRDb21NQ0JnUFJBIiwibWluaV9vcGVuaWQiOiJvaUE0UDVPTkhBcGNfZXJNbmhsSmJlenVvR0hzIiwiY2Rfb3BlbmlkIjoiIiwiYWxpcGF5X3VzZXJfaWQiOiIiLCJzdWJzY3JpYmUiOjAsIm9wZW5pZCI6Im9fVTM2c3d5WjJCV1k3dW9yQzhmeTFPWlhxcVUiLCJ3ZWNoYXRfaWQiOiI0MTY3OTQifQ.ccn_Sro7i_ybj8NLPc4V-44AIL-JKmhHeqWLR0fXbZYPId_Zkti6qrpJOtrGcMSgM68_-lZqUtSTZJ15gGPULlsyIjo9L7ykymKT1hGlcCclKzpBkXQvy-CMamRwj7mdrVsrLFv5nZANLExXGRLt6JYhQ9_rV-W8khJmhGwTVFAVDuJg_ftt5IphRM8qSaW0pIRegc-E9P3jODdhH-C1njTgH3ucAtBBsWuij_HvVXECz_OGJZdHLWYrLIpS9zuIBGYdN9t9shA6tBCs1dnszfNjzj02M9mxaQ8yZEGnOp11Q-hkRSNwq7RnAt1Yd7WwqYI3_0SteCLFomVY7u69MA",
            "inswebtoken": "",
            "x-uid": "",
            "token": "",
            "isNewToken": "true",
            "x-uuid": "59056D84338A5844BB4FCB4C9AB9B97C",
        },
        # 与就诊人相关的下单字段（按 HAR 已定位）
        "patient_id": "1851640",
        "jz_card": "0045434218",
    },
    # 通知方式
    "notify": {
        "desktop": True,
        "serverchan": {
            "enabled": True,
            "send_key": "SCT345283Tocu5Ykrf7rRtUc4de1CWCLiE",
        },
        "bark": {
            "enabled": False,
            "key": "your_bark_key",
        },
        "email": {
            "enabled": False,
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "username": "[email protected]",
            "password": "your_smtp_auth_code",
            "to": "[email protected]",
        },
    },
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clinic_monitor")

API_URL = "https://appv3-api.ynhdkc.com/apis/v1/schedule/schedules/client"


def _clean_headers(headers):
    return {k: v for k, v in headers.items() if v not in (None, "")}


def current_doctor():
    key = CONFIG.get("active_doctor")
    doctors = CONFIG.get("doctors") or {}
    if key not in doctors:
        raise KeyError(f"active_doctor={key!r} 不在 CONFIG['doctors'] 中，可选: {list(doctors)}")
    return doctors[key]


def fetch_schedules(d):
    params = {
        "current_page": 1,
        "page_size": 1000,
        "department_id": d["department_id"],
        "doctor_id": d["doctor_id"],
        "hospital_id": d["hospital_id"],
        "hospital_area_id": d["hospital_area_id"],
        "department_code": d["department_code"],
        "doctor_code": d["doctor_code"],
        "hospital_code": d["hospital_code"],
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://appv3.ynhdkc.com",
        "Referer": "https://appv3.ynhdkc.com/",
    }
    # 与占号请求使用同一套鉴权头（与浏览器一致，避免排班接口偶发未授权）
    headers.update(_clean_headers(CONFIG.get("auto_book", {}).get("headers", {})))
    resp = requests.get(API_URL, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json().get("list", [])


def format_schedule(s):
    return (
        f"{s.get('sch_date')} {s.get('time_type_text', '')} | "
        f"{s.get('doctor_name', '')} - {s.get('department_name', '')} | "
        f"剩余 {s.get('src_num')}/{s.get('max_treat_num')} | "
        f"挂号费 ¥{s.get('total_fee_format', s.get('total_fee', '?'))}"
    )


def init_csv_files():
    if not os.path.exists(CONFIG["csv_schedule_log"]):
        with open(CONFIG["csv_schedule_log"], "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                [
                    "timestamp",
                    "doctor_name",
                    "schedule_id",
                    "sch_date",
                    "time_type_text",
                    "src_num",
                    "used_num",
                    "max_treat_num",
                    "status",
                    "mode",
                ]
            )
    if not os.path.exists(CONFIG["csv_events"]):
        with open(CONFIG["csv_events"], "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                [
                    "timestamp",
                    "event_type",
                    "schedule_id",
                    "sch_date",
                    "time_type_text",
                    "src_num",
                    "max_treat_num",
                    "note",
                ]
            )
    if not os.path.exists(CONFIG["csv_book_attempts"]):
        with open(CONFIG["csv_book_attempts"], "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                [
                    "timestamp",
                    "schedule_id",
                    "sch_date",
                    "time_type_text",
                    "src_num",
                    "success",
                    "http_status",
                    "response_code",
                    "response_msg",
                ]
            )


def log_schedule_snapshot(schedules, mode):
    if not schedules:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    doctor_name = current_doctor()["doctor_name"]
    with open(CONFIG["csv_schedule_log"], "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for s in schedules:
            w.writerow(
                [
                    ts,
                    doctor_name,
                    s.get("schedule_id"),
                    s.get("sch_date"),
                    s.get("time_type_text"),
                    s.get("src_num"),
                    s.get("used_num"),
                    s.get("max_treat_num"),
                    s.get("status"),
                    mode,
                ]
            )


def log_event(event_type, schedule, note=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with open(CONFIG["csv_events"], "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(
            [
                ts,
                event_type,
                schedule.get("schedule_id"),
                schedule.get("sch_date"),
                schedule.get("time_type_text"),
                schedule.get("src_num"),
                schedule.get("max_treat_num"),
                note,
            ]
        )
    log.info(
        f"📝 事件记录: {event_type} | {schedule.get('sch_date')} "
        f"{schedule.get('time_type_text', '')} | 剩余 {schedule.get('src_num')}"
    )


def log_book_attempt(schedule, success, http_status=None, response_code=None, response_msg=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with open(CONFIG["csv_book_attempts"], "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(
            [
                ts,
                schedule.get("schedule_id"),
                schedule.get("sch_date"),
                schedule.get("time_type_text"),
                schedule.get("src_num"),
                int(bool(success)),
                http_status if http_status is not None else "",
                response_code if response_code is not None else "",
                response_msg,
            ]
        )


def notify_desktop(title, body):
    print("\a", end="", flush=True)
    print(f"\n{'=' * 60}\n  🔔 {title}\n{'=' * 60}\n{body}\n{'=' * 60}\n")
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            safe_body = body.replace('"', "'")[:200]
            os.system(
                f"""osascript -e 'display notification "{safe_body}" with title "{title}" sound name "Glass"' """
            )
        elif sysname == "Linux":
            os.system(f'notify-send "{title}" "{body[:200]}" 2>/dev/null')
        elif sysname == "Windows":
            try:
                from win10toast import ToastNotifier

                ToastNotifier().show_toast(title, body[:200], duration=10)
            except ImportError:
                pass
    except Exception as e:
        log.debug(f"桌面通知失败: {e}")


def notify_serverchan(cfg, title, body):
    try:
        url = f"https://sctapi.ftqq.com/{cfg['send_key']}.send"
        r = requests.post(url, data={"title": title, "desp": body}, timeout=10)
        log.info(f"Server酱推送: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"Server酱推送失败: {e}")


def notify_bark(cfg, title, body):
    try:
        url = f"https://api.day.app/{cfg['key']}/{title}/{body}"
        r = requests.get(url, params={"sound": "alarm"}, timeout=10)
        log.info(f"Bark 推送: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"Bark 推送失败: {e}")


def notify_email(cfg, title, body):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = cfg["username"]
        msg["To"] = cfg["to"]
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.login(cfg["username"], cfg["password"])
            s.sendmail(cfg["username"], [cfg["to"]], msg.as_string())
        log.info(f"邮件已发送至 {cfg['to']}")
    except Exception as e:
        log.warning(f"邮件发送失败: {e}")


def send_notifications(title, body):
    n = CONFIG["notify"]
    if n.get("desktop"):
        notify_desktop(title, body)
    if n.get("serverchan", {}).get("enabled"):
        notify_serverchan(n["serverchan"], title, body)
    if n.get("bark", {}).get("enabled"):
        notify_bark(n["bark"], title, body)
    if n.get("email", {}).get("enabled"):
        notify_email(n["email"], title, body)


def send_serverchan_only(title, body):
    """只发 Server酱，避免抢号状态频繁弹本地通知。"""
    sc = CONFIG.get("notify", {}).get("serverchan", {})
    if sc.get("enabled"):
        notify_serverchan(sc, title, body)


def build_book_payload(schedule):
    """
    按 HAR 里的 appointment-orders 请求结构构造占号 payload。
    """
    patient_id = CONFIG["auto_book"]["patient_id"]
    jz_card = CONFIG["auto_book"]["jz_card"]
    if not patient_id or not jz_card:
        raise ValueError("缺少 YNH_PATIENT_ID 或 YNH_JZ_CARD，无法自动占号")

    return {
        "from_channel": None,
        "department_id": str(schedule.get("department_id") or current_doctor()["department_id"]),
        "consultation_status": schedule.get("consultation_status", -1),
        "doctor_id": int(schedule.get("doctor_id") or current_doctor()["doctor_id"]),
        "hospital_area_id": int(schedule.get("hospital_area_id") or current_doctor()["hospital_area_id"]),
        "hospital_id": str(schedule.get("hospital_id") or current_doctor()["hospital_id"]),
        "jz_card": str(jz_card),
        "order_type": int(schedule.get("order_type", 0)),
        "patient_id": int(patient_id),
        "sch_date": schedule.get("sch_date"),
        "queue_sn": int(schedule.get("queue_sn", 1)),
        "end_time": schedule.get("end_time"),
        "schedule_id": str(schedule.get("schedule_id")),
        "start_time": schedule.get("start_time"),
        "time_type": int(schedule.get("time_type", 0) or 0),
        "total_fee": float(schedule.get("total_fee", 0) or 0),
        "record_type": int(schedule.get("record_type", 0)),
        "appoint_channel": schedule.get("appoint_channel"),
        "doctor_level": schedule.get("doctor_level"),
        "department_code": schedule.get("department_code") or current_doctor()["department_code"],
        "doctor_code": schedule.get("doctor_code") or current_doctor()["doctor_code"],
        "hospital_code": schedule.get("hospital_code") or current_doctor()["hospital_code"],
        "doctor_name": schedule.get("doctor_name") or current_doctor()["doctor_name"],
        "hospital_area_name": schedule.get("hospital_area_name"),
        "department_name": schedule.get("department_name"),
        "is_mi_payed": schedule.get("is_mi_payed"),
    }


def try_book_schedule(schedule):
    cfg = CONFIG["auto_book"]
    if not cfg.get("enabled"):
        return False, "auto_book disabled"
    if not cfg.get("api_url"):
        return False, "missing api_url"

    payload = build_book_payload(schedule)
    try:
        headers = _clean_headers(cfg.get("headers", {}))
        r = requests.post(
            cfg["api_url"],
            headers=headers,
            json=payload,
            timeout=cfg.get("timeout_seconds", 8),
        )
        http_status = r.status_code
        data = {}
        if r.text:
            try:
                data = r.json()
            except json.JSONDecodeError:
                data = {"msg": r.text[:300]}

        code = data.get("code")
        msg = data.get("msg") or data.get("message") or ""
        data_obj = data.get("data") if isinstance(data, dict) else {}
        data_obj = data_obj if isinstance(data_obj, dict) else {}
        has_order = bool(
            data.get("order_id")
            or data.get("order_no")
            or data_obj.get("order_id")
            or data_obj.get("order_no")
        )
        msg_text = str(msg)
        # 注意：appointment_order.400000 既可能是“自己已占号”，也可能是“被他人占用”，不能只看 code。
        already_booked = ("已经预约占号" in msg_text) or ("已预约占号" in msg_text)
        occupied_by_others = ("被其他患者占用" in msg_text) or ("号位已被占用" in msg_text)
        success = (
            (http_status == 200)
            and (
                str(code) in ("0", "200", "success", "SUCCESS")
                or data.get("success") is True
                or has_order
            )
        )

        if success:
            status_tag = "BOOKED"
        elif already_booked:
            status_tag = "ALREADY_BOOKED"
        elif occupied_by_others:
            status_tag = "OCCUPIED_BY_OTHERS"
        else:
            status_tag = "FAILED"
        log_book_attempt(schedule, success, http_status=http_status, response_code=code, response_msg=msg)
        return success, f"http={http_status}, code={code}, msg={msg}", status_tag
    except Exception as e:
        log_book_attempt(schedule, False, response_msg=str(e))
        return False, str(e), "FAILED"


def _status_priority(tag):
    order = {
        "BOOKED": 4,
        "ALREADY_BOOKED": 3,
        "OCCUPIED_BY_OTHERS": 2,
        "FAILED": 1,
    }
    return order.get(tag, 0)


def try_book_schedule_parallel(schedule):
    cfg = CONFIG["auto_book"]
    channels = int(cfg.get("parallel_channels", 1) or 1)
    channels = max(1, channels)
    stagger = float(cfg.get("parallel_stagger_seconds", 0.08) or 0.0)

    if channels == 1:
        return try_book_schedule(schedule)

    results = []
    with ThreadPoolExecutor(max_workers=channels) as pool:
        futures = []
        for idx in range(channels):
            if idx > 0 and stagger > 0:
                time.sleep(stagger)
            futures.append(pool.submit(try_book_schedule, schedule))

        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                results.append((False, str(e), "FAILED"))

    # 按优先级选最佳结果（BOOKED > ALREADY_BOOKED > OCCUPIED_BY_OTHERS > FAILED）
    best = max(results, key=lambda x: _status_priority(x[2]))
    return best


def main():
    doctor = current_doctor()
    init_csv_files()

    log.info(
        f"开始监控医生【{doctor['doctor_name']}】的号源（active_doctor={CONFIG.get('active_doctor')}）"
    )
    log.info(
        f"采样策略：平时 {CONFIG['normal_interval_seconds']}s/次，"
        f"出号后激活 {CONFIG['active_window_seconds']}s 内改为 "
        f"{CONFIG['active_interval_seconds']}s/次"
    )
    log.info(
        f"数据记录：{CONFIG['csv_schedule_log']} / {CONFIG['csv_events']} / {CONFIG['csv_book_attempts']}"
    )
    log.info("按 Ctrl+C 停止\n")

    last_notify = {}
    consecutive_errors = 0
    active_until = 0
    prev_src_num = {}
    release_seen_at = {}
    last_book_attempt = {}
    booked_success_schedules = set()
    already_booked_schedules = set()

    while True:
        try:
            schedules = fetch_schedules(doctor)
            consecutive_errors = 0
            now = time.time()
            in_active = now < active_until
            mode_label = "ACTIVE" if in_active else "NORMAL"

            log_schedule_snapshot(schedules, mode_label)

            for s in schedules:
                sid = s.get("schedule_id")
                cur = s.get("src_num", 0) or 0
                prev = prev_src_num.get(sid)

                if prev is not None:
                    if prev == 0 and cur > 0:
                        log_event("RELEASE", s, f"号源放出：0 -> {cur}")
                        release_seen_at[sid] = now
                        if not in_active:
                            active_until = now + CONFIG["active_window_seconds"]
                            log.info(
                                f"🔥 进入激活窗口（{CONFIG['active_window_seconds']}s），"
                                f"采样间隔切换为 {CONFIG['active_interval_seconds']}s"
                            )
                    elif prev > 0 and cur == 0:
                        log_event("EXHAUST", s, f"号源归零：{prev} -> 0")
                    elif cur != prev:
                        log_event("CHANGE", s, f"{prev} -> {cur}")

                prev_src_num[sid] = cur

            available = [s for s in schedules if (s.get("src_num", 0) or 0) >= CONFIG["min_remaining"]]

            # 关键改动：出号后直接抢号
            for s in available:
                sid = s.get("schedule_id")
                if sid in booked_success_schedules:
                    continue
                if sid in already_booked_schedules:
                    continue
                if now - last_book_attempt.get(sid, 0) < CONFIG["auto_book"]["retry_cooldown_seconds"]:
                    continue

                burst_window = CONFIG["auto_book"].get("burst_window_seconds", 2.0)
                in_burst_window = now - release_seen_at.get(sid, 0) <= burst_window
                burst_retry_count = int(CONFIG["auto_book"].get("burst_retry_count", 0) or 0)
                attempt_total = 1 + (burst_retry_count if in_burst_window else 0)

                for i in range(attempt_total):
                    last_book_attempt[sid] = time.time()
                    log.info(f"[{mode_label}] BOOK_ATTEMPT sid={sid} try={i+1}/{attempt_total}")
                    success, detail, status_tag = try_book_schedule_parallel(s)

                    if status_tag == "BOOKED":
                        sc_title = f"🤖 抢号状态[成功] {doctor['doctor_name']}"
                        sc_body = f"{format_schedule(s)}\n\n{detail}\n模式: {mode_label}"
                        send_serverchan_only(sc_title, sc_body)
                        booked_success_schedules.add(sid)
                        log_event("BOOK_SUCCESS", s, detail)
                        title = f"✅ 抢号成功：{doctor['doctor_name']}"
                        body = f"{format_schedule(s)}\n\n{detail}"
                        send_notifications(title, body)
                        break

                    if status_tag == "ALREADY_BOOKED":
                        already_booked_schedules.add(sid)
                        sc_title = f"🤖 抢号状态[已占号] {doctor['doctor_name']}"
                        sc_body = f"{format_schedule(s)}\n\n{detail}\n模式: {mode_label}"
                        send_serverchan_only(sc_title, sc_body)
                        log_event("BOOK_ALREADY", s, detail)
                        break

                    if status_tag == "OCCUPIED_BY_OTHERS":
                        sc_title = f"🤖 抢号状态[被占用] {doctor['doctor_name']}"
                        sc_body = f"{format_schedule(s)}\n\n{detail}\n模式: {mode_label}"
                        send_serverchan_only(sc_title, sc_body)
                        log_event("BOOK_OCCUPIED", s, detail)
                        log.info(f"[{mode_label}] 号位被他人占用: sid={sid} | {detail}")
                        if i < attempt_total - 1:
                            time.sleep(CONFIG["auto_book"].get("burst_retry_interval_seconds", 0.3))
                            continue
                        break

                    sc_title = f"🤖 抢号状态[失败] {doctor['doctor_name']}"
                    sc_body = f"{format_schedule(s)}\n\n{detail}\n模式: {mode_label}"
                    send_serverchan_only(sc_title, sc_body)
                    log_event("BOOK_FAIL", s, detail)
                    log.info(f"[{mode_label}] 自动抢号失败: sid={sid} | {detail}")

                    if i < attempt_total - 1:
                        time.sleep(CONFIG["auto_book"].get("burst_retry_interval_seconds", 0.3))

            if available:
                fresh = []
                for s in available:
                    sid = s.get("schedule_id")
                    if now - last_notify.get(sid, 0) >= CONFIG["notify_cooldown_seconds"]:
                        fresh.append(s)
                        last_notify[sid] = now

                if fresh:
                    title = f"🏥 {doctor['doctor_name']} 有号了！({len(fresh)}个)"
                    body = "\n".join(format_schedule(s) for s in fresh)
                    body += "\n\n👉 已自动执行抢号，若失败请立即手动补抢"
                    send_notifications(title, body)
                else:
                    log.info(f"[{mode_label}] 有 {len(available)} 个号源仍可预约（通知冷却中）")
            else:
                if schedules:
                    summary = ", ".join(
                        f"{s['sch_date']}{s.get('time_type_text', '')[:1]}="
                        f"{s.get('src_num', 0)}/{s.get('max_treat_num', 0)}"
                        for s in schedules[:8]
                    )
                    log.info(f"[{mode_label}] 暂无号源 [{summary}]")
                else:
                    log.info(f"[{mode_label}] 接口返回空列表")

            if active_until > 0 and now >= active_until:
                log.info("⏱ 激活窗口结束，恢复普通采样间隔")
                active_until = 0

        except requests.RequestException as e:
            consecutive_errors += 1
            log.warning(f"请求失败 ({consecutive_errors}次): {e}")
            if consecutive_errors >= 5:
                log.error("连续失败过多，等待 5 分钟")
                time.sleep(300)
                consecutive_errors = 0
                continue
        except KeyboardInterrupt:
            log.info("收到中断，退出")
            break
        except Exception as e:
            log.exception(f"未预期错误: {e}")

        sleep_sec = (
            CONFIG["active_interval_seconds"] if time.time() < active_until else CONFIG["normal_interval_seconds"]
        )
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
