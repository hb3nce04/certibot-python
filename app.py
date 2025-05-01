import logging
import os
import smtplib
import ssl
import time
from collections import Counter
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Dict

import requests
import schedule
import urllib3
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

def get_env_variable(var_name, default=None):
    value = os.getenv(var_name, default)
    if value is None:
        print(f"Warning: Environment variable '{var_name}' is not set.")
    return value


load_dotenv()
filename = 'timestamp.txt'
timestamp_format = '%Y-%m-%d %H:%M:%S'
env = get_env_variable('ENV', 'prod')
username = get_env_variable('NEPTUN_USERNAME')
password = get_env_variable('NEPTUN_PASSWORD')
service_email = get_env_variable('SERVICE_EMAIL')
run_minutes = int(os.getenv("RUN_MINUTES", 1))
month_to_check = int(get_env_variable('MONTH_TO_CHECK', 1))
resend_in_days = int(get_env_variable('RESEND_IN_DAYS', 3))
smtp_server = "smtp.gmail.com"
port = 465
email_password = get_env_variable('EMAIL_APP_PASSWORD')
email_list = os.getenv("EMAIL_LIST", "")
recipients = [email.strip() for email in email_list.split(",") if email.strip()]

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt=timestamp_format,
    level=logging.INFO if env == "prod" else logging.DEBUG
)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def setup_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument('--ignore-ssl-errors=yes')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(options=options)


def login(driver: webdriver.Chrome, username: str, password: str) -> None:
    wait = WebDriverWait(driver, 10)

    driver.get("https://icert.inf.unideb.hu/home")

    wait.until(EC.element_to_be_clickable((By.NAME, "submitB"))).click()
    wait.until(EC.presence_of_element_located((By.NAME, "username"))).send_keys(username)
    wait.until(EC.presence_of_element_located((By.NAME, "password"))).send_keys(password)
    wait.until(EC.element_to_be_clickable((By.NAME, "login"))).click()

    wait.until(EC.text_to_be_present_in_element((By.XPATH, '//span[text()="Exams"]'), "Exams"))
    driver.find_element(By.XPATH, '//span[text()="Exams"]').click()


def get_exam_data(cookies: Dict[str, str]) -> Dict:
    now = datetime.now(timezone.utc)
    than = now + relativedelta(months=month_to_check)
    formatted_now = now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    formatted_then = than.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    api_url = ('https://icert.inf.unideb.hu/api/getEventsBetweenDates?'
               'startDate=' + formatted_now + '&endDate=' + formatted_then)
    response = requests.get(api_url, cookies=cookies, verify=False)
    response.raise_for_status()
    return response.json()


def analyze_exam_data_and_send_email(exam_data: list) -> None:
    body_lines = []

    status_counts = Counter(exam['eventStatus'] for exam in exam_data)
    active_count = status_counts.get('active', 0)
    body_lines.append(f"Aktív vizsgák: {active_count}")

    total_capacity = sum(exam['maxAttendance'] for exam in exam_data)
    total_attendances = sum(exam['_count']['eventAttendances'] for exam in exam_data)
    body_lines.append(f"Összes férőhely: {total_capacity}")
    body_lines.append(f"Összes jelentkező: {total_attendances}\n")

    sorted_exams = sorted(exam_data, key=lambda x: x['date'])

    body_lines.append("Vizsgaidőpontok:")
    for exam in sorted_exams:
        date = datetime.fromisoformat(exam['date'].replace('Z', '+00:00'))
        status = "✓" if exam['eventStatus'] == 'completed' else "⌛"
        attendees = exam['_count']['eventAttendances']
        max_att = exam['maxAttendance']
        body_lines.append(f"{status} {date.strftime('%Y-%m-%d %H:%M')} - "
                          f"Jelentkezők: {attendees}/{max_att}")

    available_exams = [
        exam for exam in exam_data
        if exam['eventStatus'] == 'active' and exam['_count']['eventAttendances'] < exam['maxAttendance'] and
           exam['_count']['eventAttendances'] < exam['maxAttendance']]

    if available_exams:
        logging.info("Új vizsgakiírás létezik.")
        if is_email_send_necessary():
            logging.info("E-mail küldése szükséges")
            body_lines.append("\nSzabad helyek:")
            for exam in available_exams:
                date = datetime.fromisoformat(exam['date'].replace('Z', '+00:00'))
                free_spots = exam['maxAttendance'] - exam['_count']['eventAttendances']
                body_lines.append(f"{date.strftime('%Y-%m-%d %H:%M')} - {free_spots} szabad hely")
            email_body = "\n".join(body_lines)
            send_email(email_body, recipients)
            save_timestamp_of_sending()
        else:
            logging.info("E-mail küldése nem szükséges")
    else:
        logging.info("Nincs új kiírás, e-mail küldés nem történt.")

def is_email_send_necessary():
    now = datetime.now()
    if not os.path.exists(filename):
        return True
    with open(filename, 'r') as f:
        timestamp_str = f.read().strip()
        latest_save_time = datetime.strptime(timestamp_str, timestamp_format)
    latest_save = latest_save_time + relativedelta(days=resend_in_days)
    return now > latest_save

def save_timestamp_of_sending():
    with open(filename, 'w') as f:
        f.write(datetime.now().strftime(timestamp_format))
        logging.debug("Mentés időpontja feljegyezve/módosítva")


def send_email(body: str, addresses: list[str] = None) -> None:
    if addresses is None:
        addresses = [service_email]
    msg = EmailMessage()
    msg["Subject"] = "CERTIPORT VIZSGAÉRTESÍTÉS"
    msg["From"] = service_email

    full_body = f'{body}\n\nCERTIPORT BOT powered by: Hagymási Bence'
    msg.set_content(full_body, charset="utf-8")

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(smtp_server, port, context=context) as server:
            server.login(service_email, email_password)
            server.send_message(msg, to_addrs=addresses)
            logging.debug(f"Email küldve az alábbiaknak: {','.join(addresses)}")
    except Exception as e:
        print(f"Hiba történt az e-mail küldése során {e}")


def main():
    driver = setup_driver()
    try:
        login(driver, username, password)
        cookies = {cookie['name']: cookie['value'] for cookie in driver.get_cookies()}
        logging.info("Adatok letöltése...")
        exam_data = get_exam_data(cookies)
        logging.info("Adatok elemzése...")
        analyze_exam_data_and_send_email(exam_data)
    except Exception as e:
        print(f"Hiba történt: {e}")
    finally:
        driver.quit()

schedule.every(run_minutes).minutes.do(main)

if __name__ == "__main__":
    logging.info("Rendszer elindítva!")
    logging.info("env=" + str(env))
    logging.info("minutes timing=" + str(run_minutes))
    logging.info("resend day=" + str(resend_in_days))
    logging.info("month to check=" + str(month_to_check))
    logging.info("recepients=" + ','.join(recipients))
    email_body = f"""
        Environment: {env}
        Minutes Timing: {run_minutes}
        Resend every nth day: {resend_in_days}
        Service Email: {service_email}
        Month to Check: {month_to_check}
        Recipients: {', '.join(recipients)}
        """
    if env == "prod":
        send_email('A rendszer elindult!\n'+email_body)
    main()
    while True:
        schedule.run_pending()
        time.sleep(1)
