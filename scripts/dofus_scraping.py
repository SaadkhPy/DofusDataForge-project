import os
import re
import logging
import random
import requests
import pandas as pd
import psycopg2
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from urllib.parse import urljoin

# ========== Config ==========
BASE_URL = "https://www.dofus-touch.com/fr/mmorpg/encyclopedie/monstres?text=&monster_level_min=1&monster_level_max=1200&monster_type[0]=archimonster"
DB_NAME = "dofus_user"
DB_USER = "dofus_user"
DB_PASS = "dofus_pass"
DB_HOST = "db"
DB_PORT = "5432"
PAGES_TO_SCRAPE = 12
DOWNLOAD_DIR = "/app/download/Images"
EXPORT_DIR = "/app/download"
CSV_PATH = "download/archimonsters.csv"
IMAGE_FOLDER = "download/Images"
EXPORT_DIR = "download"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ========== Logging ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def setup_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_").strip()

def get_extension_from_url(url):
    basename = url.split("/")[-1].split("?")[0]
    return os.path.splitext(basename)[1] or ".png"

def download_image(url, monster_name):
    safe_name = sanitize_filename(monster_name)
    ext = get_extension_from_url(url)
    filepath = os.path.join(DOWNLOAD_DIR, f"{safe_name}{ext}")
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(response.content)
        return filepath
    except Exception as e:
        logging.warning(f"⚠️ Failed to download image for '{monster_name}': {e}")
        return ""

def get_page_html(driver, page_number):
    url = f"{BASE_URL}&page={page_number}"
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        return BeautifulSoup(driver.page_source, "html.parser")
    except TimeoutException:
        logging.warning(f"⚠️ Timeout on page {page_number}")
        return BeautifulSoup("", "html.parser")

def extract_monsters(soup):
    table = soup.find("table")
    if not table:
        return []

    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    name_idx = headers.index("nom") if "nom" in headers else None
    level_idx = headers.index("niveau") if "niveau" in headers else None

    if name_idx is None:
        return []

    monsters = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) <= name_idx:
            continue

        name = cells[name_idx].get_text(strip=True)
        level = cells[level_idx].get_text(strip=True) if level_idx is not None else ""
        img_tag = cells[0].find("img")
        raw_img_url = img_tag["src"] if img_tag and "src" in img_tag.attrs else ""
        img_url = (
            f"https:{raw_img_url}" if raw_img_url.startswith("//")
            else f"https://static.ankama.com{raw_img_url}" if raw_img_url.startswith("/")
            else urljoin("https://static.ankama.com/", raw_img_url)
        )

        local_image = download_image(img_url, name) if img_url else ""
        monsters.append({
            "name": name,
            "level": level,
            "url_image": img_url,
            "local_image": local_image
        })

    return monsters

def save_to_postgres(df):
    try:
        with psycopg2.connect(
            dbname=DB_NAME, user=DB_USER, password=DB_PASS,
            host=DB_HOST, port=DB_PORT
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS archimonsters (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        level TEXT,
                        url_image TEXT,
                        local_image TEXT,
                        UNIQUE(name, url_image)
                    );
                """)
                for _, row in df.iterrows():
                    cur.execute("""
                        INSERT INTO archimonsters (name, level, url_image, local_image)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (name, url_image) DO NOTHING;
                    """, (row["name"], row["level"], row["url_image"], row["local_image"]))
                conn.commit()
        logging.info("✅ Data saved to PostgreSQL")
    except Exception as e:
        logging.error(f"❌ PostgreSQL error: {e}")

def populate_user_monsters(df):
    users = ['user_1', 'user_2']
    sample_names = df["name"].tolist()

    try:
        with psycopg2.connect(
            dbname=DB_NAME, user=DB_USER, password=DB_PASS,
            host=DB_HOST, port=DB_PORT
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_monsters (
                        id SERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        monster_name TEXT NOT NULL,
                        quantity INTEGER DEFAULT 0,
                        UNIQUE(user_id, monster_name)
                    );
                """)
                for user in users:
                    user_sample = random.sample(sample_names, min(12, len(sample_names)))
                    for name in user_sample:
                        qty = random.randint(1, 5)
                        cur.execute("""
                            INSERT INTO user_monsters (user_id, monster_name, quantity)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (user_id, monster_name) DO UPDATE
                            SET quantity = EXCLUDED.quantity;
                        """, (user, name, qty))
                conn.commit()
        logging.info("🧪 Sample user data inserted.")
    except Exception as e:
        logging.error(f"❌ Error inserting ownership data: {e}")

def run_scraper(pages=PAGES_TO_SCRAPE):
    driver = setup_driver()
    all_monsters = []
    try:
        for i in range(1, pages + 1):
            logging.info(f"🔍 Scraping page {i}...")
            soup = get_page_html(driver, i)
            all_monsters.extend(extract_monsters(soup))
    finally:
        driver.quit()
    return pd.DataFrame(all_monsters)

def full_scrape_and_save():
    df = run_scraper()
    if not df.empty:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        df.to_csv(os.path.join(EXPORT_DIR, "archimonsters.csv"), index=False)
        df.to_json(os.path.join(EXPORT_DIR, "archimonsters.json"), orient="records", indent=2)
        save_to_postgres(df)
        populate_user_monsters(df)
        logging.info(f"✅ Total monsters scraped: {len(df)}")
    else:
        logging.warning("⚠️ No data scraped.")
