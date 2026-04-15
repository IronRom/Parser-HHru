#!/usr/bin/env python3
"""hh.ru vacancy scraper — сохраняет вакансии как .md файлы."""

import argparse
import os
import re
import time
import sys
from html.parser import HTMLParser
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv не обязателен

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    print("Установите зависимости: pip install selenium webdriver-manager python-dotenv")
    sys.exit(1)

HH_BASE = "https://hh.ru"

# Популярные регионы hh.ru: название → ID
AREAS: dict[str, int] = {
    "россия":           113,
    "москва":             1,
    "санкт-петербург":    2,
    "спб":                2,
    "екатеринбург":       3,
    "новосибирск":        4,
    "казань":            88,
    "нижний новгород":   66,
    "самара":            78,
    "краснодар":         53,
    "ростов-на-дону":    76,
    "уфа":               99,
    "омск":              68,
    "красноярск":        54,
    "пермь":             72,
    "воронеж":           26,
    "волгоград":         24,
    "тюмень":            95,
    "минск":           1002,
    "алматы":           160,
    "киев":             115,
}

# Форматы работы (параметр schedule в URL)
SCHEDULES: dict[str, str] = {
    "полный день":      "fullDay",
    "full":             "fullDay",
    "fullday":          "fullDay",
    "сменный":          "shift",
    "shift":            "shift",
    "гибкий":           "flexible",
    "flexible":         "flexible",
    "удалённо":         "remote",
    "remote":           "remote",
    "вахта":            "flyInFlyOut",
    "fly":              "flyInFlyOut",
}

# Типы занятости (параметр employment в URL)
EMPLOYMENTS: dict[str, str] = {
    "полная":       "full",
    "full":         "full",
    "частичная":    "part",
    "part":         "part",
    "проект":       "project",
    "project":      "project",
    "стажировка":   "probation",
    "probation":    "probation",
    "волонтёр":     "volunteer",
    "volunteer":    "volunteer",
}


def resolve_areas(raw: str) -> list[int]:
    """Преобразует строку с регионами (имена или ID через запятую) в список ID."""
    ids = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            ids.append(int(token))
        else:
            key = token.lower()
            if key in AREAS:
                ids.append(AREAS[key])
            else:
                print(f"  Предупреждение: регион «{token}» не найден, пропускаем")
    return ids or [113]  # по умолчанию — вся Россия


def resolve_schedules(raw: str) -> list[str]:
    """Преобразует строку с форматами работы в список slug-ов для URL."""
    slugs = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        key = token.lower()
        if key in SCHEDULES:
            slugs.append(SCHEDULES[key])
        elif token in SCHEDULES.values():
            slugs.append(token)
        else:
            print(f"  Предупреждение: формат работы «{token}» не распознан, пропускаем")
    return slugs


def resolve_employment(raw: str) -> str | None:
    """Преобразует строку типа занятости в slug для URL."""
    key = raw.strip().lower()
    if key in EMPLOYMENTS:
        return EMPLOYMENTS[key]
    if raw.strip() in EMPLOYMENTS.values():
        return raw.strip()
    print(f"  Предупреждение: тип занятости «{raw}» не распознан, игнорируем")
    return None


def build_search_url(keyword: str, areas: list[int], page: int,
                     schedules: list[str], employment: str | None) -> str:
    params = [f"text={keyword.replace(' ', '+')}"]
    for area_id in areas:
        params.append(f"area={area_id}")
    params.append("per_page=20")
    params.append(f"page={page}")
    for sched in schedules:
        params.append(f"schedule={sched}")
    if employment:
        params.append(f"employment={employment}")
    return f"{HH_BASE}/search/vacancy?" + "&".join(params)


def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._block_tags = {
            "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
            "tr", "td", "th",
        }

    def handle_starttag(self, tag, attrs):
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def handle_entityref(self, name):
        entities = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"'}
        self._parts.append(entities.get(name, ""))

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    parser = HTMLToText()
    parser.feed(html)
    return parser.get_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:80]


def format_salary(salary: dict | None) -> str:
    if not salary:
        return "не указана"
    parts = []
    if salary.get("from"):
        parts.append(f"от {salary['from']}")
    if salary.get("to"):
        parts.append(f"до {salary['to']}")
    currency = salary.get("currency", "")
    gross = " (до вычета налогов)" if salary.get("gross") else " (на руки)"
    return " ".join(parts) + f" {currency}{gross}" if parts else "не указана"


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_vacancies(
    driver: webdriver.Chrome,
    keyword: str,
    pages: int,
    areas: list[int],
    schedules: list[str],
    employment: str | None,
) -> list[dict]:
    """Собирает ссылки на вакансии со страниц поиска."""
    results = []
    wait = WebDriverWait(driver, 15)

    for page in range(pages):
        url = build_search_url(keyword, areas, page, schedules, employment)
        driver.get(url)

        try:
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy']")
            ))
        except TimeoutException:
            print(f"  Страница {page + 1}: вакансии не найдены или таймаут")
            break

        cards = driver.find_elements(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy']")
        page_items = []
        for card in cards:
            try:
                link_el = card.find_element(By.CSS_SELECTOR, "[data-qa='serp-item__title']")
                href = link_el.get_attribute("href")
                clean_url = href.split("?")[0]
                vid = clean_url.rstrip("/").split("/")[-1]
                page_items.append({"id": vid, "url": clean_url})
            except NoSuchElementException:
                continue

        results.extend(page_items)
        print(f"  Страница {page + 1}: получено {len(page_items)} вакансий")

        if len(page_items) < 20:
            break
        time.sleep(1.0)

    return results


def fetch_vacancy_detail(driver: webdriver.Chrome, vacancy: dict) -> dict:
    """Открывает страницу вакансии и извлекает все данные."""
    wait = WebDriverWait(driver, 15)
    driver.get(vacancy["url"])

    try:
        wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "h1[data-qa='vacancy-title']")
        ))
    except TimeoutException:
        return {}

    def text(selector: str, default: str = "") -> str:
        try:
            return driver.find_element(By.CSS_SELECTOR, selector).text.strip()
        except NoSuchElementException:
            return default

    def attr(selector: str, attribute: str, default: str = "") -> str:
        try:
            return driver.find_element(By.CSS_SELECTOR, selector).get_attribute(attribute) or default
        except NoSuchElementException:
            return default

    title = text("h1[data-qa='vacancy-title']")
    salary_text = (
        text("[data-qa='vacancy-salary']")
        or text("[data-qa='vacancy-salary-compensation-type-net']")
    )
    employer = (
        text("[data-qa='vacancy-company-name']")
        or text("[data-qa='bloko-header-2']")
    )
    area = (
        text("[data-qa='vacancy-view-location']")
        or text("[data-qa='vacancy-view-raw-address']")
    )

    experience = ""
    employment = ""
    schedule = ""
    try:
        mode_els = driver.find_elements(
            By.CSS_SELECTOR, "[data-qa='vacancy-view-employment-mode']"
        )
        for el in mode_els:
            t = el.text.strip()
            if t:
                if not experience:
                    experience = t
                elif not employment:
                    employment = t
    except NoSuchElementException:
        pass

    published_raw = attr("time[itemprop='datePosted']", "datetime")
    published_at = published_raw[:10] if published_raw else ""

    skills = []
    try:
        skill_els = driver.find_elements(By.CSS_SELECTOR, "[data-qa='bloko-tag__text']")
        skills = [{"name": el.text.strip()} for el in skill_els if el.text.strip()]
    except NoSuchElementException:
        pass

    description = ""
    try:
        desc_el = driver.find_element(By.CSS_SELECTOR, "[data-qa='vacancy-description']")
        description = desc_el.get_attribute("innerHTML") or ""
    except NoSuchElementException:
        pass

    return {
        "name": title,
        "employer": {"name": employer},
        "area": {"name": area},
        "salary_text": salary_text,
        "experience": {"name": experience},
        "employment": {"name": employment},
        "schedule": {"name": schedule},
        "published_at": published_at,
        "alternate_url": vacancy["url"],
        "key_skills": skills,
        "description": description,
    }


def vacancy_to_md(v: dict) -> str:
    title = v.get("name", "Без названия")
    employer = (v.get("employer") or {}).get("name", "Не указана")
    area = (v.get("area") or {}).get("name", "Не указан")
    salary = v.get("salary_text") or format_salary(v.get("salary"))
    experience = (v.get("experience") or {}).get("name", "Не указан")
    employment = (v.get("employment") or {}).get("name", "Не указана")
    schedule = (v.get("schedule") or {}).get("name", "Не указан")
    published_at = (v.get("published_at") or "")[:10]
    url = v.get("alternate_url", "")

    skills = v.get("key_skills", [])
    skills_md = "\n".join(f"- {s['name']}" for s in skills) if skills else "_не указаны_"

    description_html = v.get("description", "")
    description = html_to_text(description_html) if description_html else "_не указано_"

    lines = [
        f"# {title}",
        "",
        f"**Компания:** {employer}",
        f"**Зарплата:** {salary}",
        f"**Город:** {area}",
        f"**Опыт:** {experience}",
        f"**Занятость:** {employment}",
        f"**График:** {schedule}",
        f"**Опубликовано:** {published_at}",
        f"**Ссылка:** {url}",
        "",
        "---",
        "",
        "## Ключевые навыки",
        "",
        skills_md,
        "",
        "---",
        "",
        "## Описание",
        "",
        description,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="hh.ru scraper — сохраняет вакансии как .md файлы",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python hh_scraper.py "devops engineer"
  python hh_scraper.py "python developer" --areas "москва,спб" --schedule remote
  python hh_scraper.py "qa engineer" --areas "1,2,3" --employment full --pages 3

Форматы работы (--schedule):
  remote, fullday, shift, flexible, fly

Типы занятости (--employment):
  full, part, project, probation, volunteer

Регионы (--areas) — имя или ID через запятую:
  россия, москва, спб, екатеринбург, новосибирск, казань, краснодар, ...
  Или числовой ID из hh.ru (например: 1,2,3)
        """,
    )
    parser.add_argument("keyword", nargs="?", help="Ключевые слова для поиска")
    parser.add_argument("--pages", type=int, help="Количество страниц (макс 50)")
    parser.add_argument("--output", help="Папка для сохранения файлов")
    parser.add_argument(
        "--areas",
        help='Регионы через запятую: "москва,спб" или "1,2,113"',
    )
    parser.add_argument(
        "--schedule",
        help='Формат работы через запятую: "remote,fullday"',
    )
    parser.add_argument(
        "--employment",
        help='Тип занятости: full | part | project | probation | volunteer',
    )
    args = parser.parse_args()

    # Приоритет: аргументы CLI > .env > интерактивный ввод > значения по умолчанию
    keyword = args.keyword or os.getenv("HH_KEYWORD", "").strip()
    pages_raw = args.pages or os.getenv("HH_PAGES", "1")
    output_raw = args.output or os.getenv("HH_OUTPUT", "vacancies")
    areas_raw = args.areas or os.getenv("HH_AREAS", "россия")
    schedule_raw = args.schedule or os.getenv("HH_SCHEDULE", "")
    employment_raw = args.employment or os.getenv("HH_EMPLOYMENT", "")

    print("=== hh.ru Scraper ===\n")

    if not keyword:
        keyword = input("Ключевые слова для поиска: ").strip()
    if not keyword:
        print("Ключевые слова не введены. Выход.")
        sys.exit(1)

    pages = max(1, min(int(pages_raw), 50))
    output_dir = Path(output_raw)
    output_dir.mkdir(parents=True, exist_ok=True)

    areas = resolve_areas(areas_raw)
    schedules = resolve_schedules(schedule_raw) if schedule_raw else []
    employment = resolve_employment(employment_raw) if employment_raw else None

    area_names = areas_raw
    sched_names = schedule_raw or "любой"
    empl_name = employment_raw or "любая"

    print(f"  Запрос:     {keyword}")
    print(f"  Регионы:    {area_names}  →  IDs: {areas}")
    print(f"  График:     {sched_names}")
    print(f"  Занятость:  {empl_name}")
    print(f"  Страниц:    {pages}")
    print(f"  Папка:      {output_dir.resolve()}")
    print()

    print("Запускаем браузер...")
    driver = make_driver()
    try:
        print(f"\nИщем вакансии по запросу «{keyword}»...\n")
        vacancies = fetch_vacancies(driver, keyword, pages, areas, schedules, employment)
        print(f"\nНайдено {len(vacancies)} вакансий. Загружаем детали...\n")

        saved = 0
        for i, v in enumerate(vacancies, 1):
            try:
                detail = fetch_vacancy_detail(driver, v)
                time.sleep(0.5)
            except Exception as e:
                print(f"  [{i}/{len(vacancies)}] Ошибка: {e}")
                continue

            if not detail:
                print(f"  [{i}/{len(vacancies)}] Пустой ответ, пропускаем")
                continue

            md_content = vacancy_to_md(detail)
            vtitle = detail.get("name", "vacancy")
            vcompany = (detail.get("employer") or {}).get("name", "company")
            published = (detail.get("published_at") or "")[:10]

            filename = f"{published}_{sanitize_filename(vcompany)}_{sanitize_filename(vtitle)}.md"
            filepath = output_dir / filename
            filepath.write_text(md_content, encoding="utf-8")
            print(f"  [{i}/{len(vacancies)}] Сохранено: {filename}")
            saved += 1
    finally:
        driver.quit()

    print(f"\nГотово! Сохранено {saved} файлов в {output_dir.resolve()}")


if __name__ == "__main__":
    main()
