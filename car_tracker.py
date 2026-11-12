import os
import time
import math
import sqlite3
from datetime import date

import requests
from bs4 import BeautifulSoup

# SETTINGS

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cars.db")

REQUEST_TIMEOUT = 15          # seconds to wait for a response
MAX_RETRIES = 3               # attempts per page before giving up
PAGE_DELAY = 1                # seconds between pages, to be polite to the site
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mk-car-tracker/1.0)"}

MIN_YEAR = 1990
MAX_YEAR = date.today().year + 1   # allow next year's models
MIN_PRICE, MAX_PRICE = 500, 80000

# Sends a few "." messages at the start of each run to visually separate runs in Discord
CLEAR_DISCORD_ON_START = True

SEARCH_URL = (
    "https://reklama5.mk/Search?city=0&cat=24&q=&f31=&priceFrom=&priceTo="
    "&f33_from=&f33_to=&f36_from=&f36_to=&f35=&f37=&f138=&f10016_from="
    "&f10016_to=&f10042=&sell=0&sell=1&buy=0&buy=1&trade=0&trade=1"
    "&includeOld=0&includeOld=1&includeNew=0&includeNew=1&cargoReady=0"
    "&DDVIncluded=0&private=0&company=0&page={page}&SortByPrice=0&zz=1&pageView="
)

# LOOKUPS

make_map = {
    "VW": "Volkswagen",
    "VOLKSWAGEN": "Volkswagen",
    "MERCEDESBENZ": "Mercedes",
    "MERCEDES": "Mercedes",
    "CITROEN": "Citroen",
    "HYUNDAI": "Hyundai",
    "SAAB": "Saab",
    "BMW": "BMW",
    "AUDI": "Audi",
    "OPEL": "Opel",
    "FORD": "Ford",
    "KIA": "Kia",
    "SEAT": "Seat",
    "SKODA": "Skoda",
    "RENAULT": "Renault",
    "PEUGEOT": "Peugeot",
    "TOYOTA": "Toyota",
    "HONDA": "Honda",
    "NISSAN": "Nissan",
    "MITSUBISHI": "Mitsubishi",
    "FIAT": "Fiat",
    "SUZUKI": "Suzuki",
    "ALFA": "Alfa Romeo",
    "LANDROVER": "Land Rover",
    "DS": "DS Automobiles",
}

two_word_makes = ["Alfa Romeo", "Land Rover", "DS Automobiles"]

known_makes = [
    "Abarth", "Acura", "Aixam", "Alfa Romeo", "Aston Martin", "Audi",
    "Bentley", "Buick", "BMW", "BYD", "Cadillac", "Chevrolet", "Chrysler",
    "Citroen", "Dacia", "Cupra", "Daewoo", "Daihatsu", "Dodge",
    "DS Automobiles", "Ferrari", "Fiat", "Ford", "Honda", "Hummer",
    "Hyundai", "Infiniti", "Isuzu", "Jaguar", "Jeep", "Kia", "Lada",
    "Lamborghini", "Lancia", "LandRover", "Lexus", "Liger", "Lotus",
    "Maserati", "Maybach", "Mazda", "Mercedes-Benz", "MG", "Mini",
    "Mitsubishi", "Nissan", "Opel", "Peugeot", "Pontiac", "Porsche",
    "Proton", "Renault", "Rolls-Royce", "Rover", "Saab", "Seat", "Skoda",
    "Smart", "Ssangyong", "Subaru", "Suzuki", "Tesla", "Toyota",
    "VW Volkswagen", "Volvo", "Wartburg", "Yugo", "Zastava",
    "Land Rover"
]

known_makes_upper = [m.upper().replace("-", "").replace(" ", "") for m in known_makes]

# DATABASE

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            make TEXT,
            model TEXT,
            year INTEGER,
            km INTEGER,
            hp INTEGER,
            price INTEGER,
            listing_url TEXT UNIQUE,
            date_posted TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            make TEXT,
            model TEXT,
            year INTEGER,
            km INTEGER,
            price INTEGER,
            median_price INTEGER,
            deal_score INTEGER,
            confidence INTEGER,
            listing_url TEXT UNIQUE,
            date_found TEXT
        )
    """)
    conn.commit()
    return conn


def insert_car(conn, car):
    cursor = conn.execute(
        "INSERT OR IGNORE INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (car['make'].title(), car['model'].title(), car['year'], car['km'],
         car['hp'], car['price'], car['listing_url'], car['date_posted'])
    )
    return cursor.rowcount > 0

# DISCORD

def post_to_discord(message):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"Could not send Discord message: {e}")


def send_deal_alert(car, median_price, deal_score, confidence):
    saving = median_price - car['price']
    saving_pct = round((saving / median_price) * 100)
    message = (
        f"🔥 Score: {deal_score} | Confidence: {confidence}% | {car['make']} {car['model']} {car['year']}\n"
        f"💰 {car['price']}€ — saves {saving}€ ({saving_pct}% below median {median_price}€)\n"
        f"📍 {car['km']}km | {car['date_posted']}\n"
        f"🔗 {car['listing_url']}"
    )
    post_to_discord(message)


def clear_discord():
    for _ in range(10):
        post_to_discord(".")

# DEAL DETECTION

def check_deal(conn, car, mode):
    """Scores a listing against comparable cars. Returns True if it is a deal."""
    rows = conn.execute("""
        SELECT price FROM listings
        WHERE make = ? AND model = ?
        AND year BETWEEN ? AND ?
        AND km BETWEEN ? AND ?
        AND listing_url != ?
    """, (car['make'].title(), car['model'].title(), car['year'] - 2, car['year'] + 2,
          car['km'] - 50000, car['km'] + 50000, car['listing_url'])).fetchall()

    count = len(rows)
    if count < 5:
        return False

    prices = sorted([r[0] for r in rows])

    # trim top and bottom 10% to remove fake/outlier listings
    trim = max(1, int(len(prices) * 0.1))
    trimmed = prices[trim:-trim]

    # trimmed median
    mid = len(trimmed) // 2
    if len(trimmed) % 2 == 0:
        median_price = (trimmed[mid - 1] + trimmed[mid]) / 2
    else:
        median_price = trimmed[mid]

    # smooth confidence curve
    confidence = 1 - math.exp(-count / 15)

    # volatility penalty
    if len(trimmed) >= 2:
        mean = sum(trimmed) / len(trimmed)
        std_dev = (sum((p - mean) ** 2 for p in trimmed) / len(trimmed)) ** 0.5
        volatility = std_dev / median_price
        volatility_penalty = max(0, 1 - volatility)
    else:
        volatility_penalty = 1

    final_confidence = confidence * volatility_penalty

    saving = median_price - car['price']
    saving_pct = (saving / median_price) * 100
    deal_score = round(saving_pct - (car['km'] / 10000))
    final_score = round(deal_score * final_confidence)

    if not (car['price'] < median_price * 0.8 and saving > 1000 and final_score > 0):
        return False

    # store the deal; rowcount tells us whether it was seen before
    is_new_deal = conn.execute(
        "INSERT OR IGNORE INTO deals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, DATE('now'))",
        (car['make'].title(), car['model'].title(), car['year'], car['km'],
         car['price'], round(median_price), final_score, round(final_confidence * 100),
         car['listing_url'])
    ).rowcount > 0
    conn.commit()

    if mode == 1 or (mode == 2 and is_new_deal):
        send_deal_alert(car, round(median_price), final_score, round(final_confidence * 100))
    return True

# SCRAPING

def strip_prefix(text):
    prefixes = ["се продава ", "se prodava ", "продавам ", "prodavam "]
    lower = text.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            return text[len(prefix):]
    return text


def fetch_page(page):
    """Downloads one search results page, retrying on network errors. Returns None on failure."""
    url = SEARCH_URL.format(page=page)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code in (401, 403):
                print(f"Page {page}: access denied by the site (HTTP {response.status_code}).")
                return None
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as e:
            print(f"Page {page}: attempt {attempt} failed ({e})")
            time.sleep(2 * attempt)
    return None


def parse_listing(title, desc_div, price):
    """Turns one listing's HTML into a car dict, or None if it can't be parsed."""
    try:
        title_parts = strip_prefix(title.text.strip()).split()
        make = title_parts[0].replace("-", "")
        make = make_map.get(make.upper(), make.title())
        # handle two-word makes
        model = title_parts[2] if make in two_word_makes else title_parts[1]
    except IndexError:
        return None

    if make.upper().replace("-", "").replace(" ", "") not in known_makes_upper:
        return None

    listing_url = "https://reklama5.mk" + title["href"]

    date_span = desc_div.find("span", style=lambda s: s and "float:right" in s.replace(" ", ""))
    listing_date = date_span.text.strip().replace("\n", " ") if date_span else None

    p = desc_div.find("p")
    if not p:
        return None
    try:
        specs_parts = " ".join(p.stripped_strings).split("•")
        year = int(specs_parts[0].strip())
        km = int(specs_parts[1].replace("km", "").replace(",", "").strip())
        hp = int(specs_parts[2].split("(")[1].replace("Hp)", "").strip())
    except (ValueError, IndexError):
        return None
    if year < MIN_YEAR or year > MAX_YEAR:
        return None

    try:
        price_text = price.get_text(separator=" ", strip=True)
        value = int(price_text.replace("€", "").replace(".", "").strip())
    except ValueError:
        return None
    if value < MIN_PRICE or value > MAX_PRICE:
        return None

    return {
        "make": make,
        "model": model,
        "year": year,
        "km": km,
        "hp": hp,
        "price": value,
        "listing_url": listing_url,
        "date_posted": listing_date,
    }


def ask_mode():
    print("Select mode:")
    print("  1 - Full scrape, send ALL deals (including already seen)")
    print("  2 - Full scrape, send only NEW deals (first time seen)")
    while True:
        try:
            mode = int(input("Enter 1 or 2: ").strip())
            if mode in (1, 2):
                return mode
        except ValueError:
            pass
        print("Please enter 1 or 2.")


def main():
    if not DISCORD_WEBHOOK:
        print("Warning: DISCORD_WEBHOOK is not set, deals will be saved but no alerts sent.")

    mode = ask_mode()
    conn = init_db()

    if CLEAR_DISCORD_ON_START:
        clear_discord()

    page = 1
    total_scraped = total_inserted = total_deals = 0

    while True:
        soup = fetch_page(page)
        if soup is None:
            print(f"Stopping: page {page} could not be downloaded.")
            break

        titles_car = soup.find_all("a", attrs={"class": "SearchAdTitle"})
        car_desc_divs = soup.find_all("div", attrs={"class": "ad-desc-div"})
        prices = soup.find_all("span", attrs={"class": "search-ad-price"})

        if not titles_car:
            break

        for title, desc_div, price in zip(titles_car, car_desc_divs, prices):
            car = parse_listing(title, desc_div, price)
            if car is None:
                continue

            total_scraped += 1
            if insert_car(conn, car):
                total_inserted += 1
            if check_deal(conn, car, mode):
                total_deals += 1

        conn.commit()
        print(f"Page {page} done")
        page += 1
        time.sleep(PAGE_DELAY)

    conn.commit()
    conn.close()
    print(f"Scraping Done! Scraped: {total_scraped} | Inserted: {total_inserted} | Deals found: {total_deals}")


if __name__ == "__main__":
    main()
