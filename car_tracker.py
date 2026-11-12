import os
import time
import math
from bs4 import BeautifulSoup
import requests
import sqlite3

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

# DB SETUP

conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "cars.db"))
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

# ── ADDED: deals table ──────────────────────────────────────────────────────
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
# ───────────────────────────────────────────────────────────────────────────

conn.commit()

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

# HELPERS

total_deals = 0

def strip_prefix(text):
    prefixes = ["се продава ", "se prodava ", "продавам ", "prodavam "]
    lower = text.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            return text[len(prefix):]
    return text

def send_deal_alert(car, median_price, deal_score, confidence):
    saving = median_price - car['price']
    saving_pct = round((saving / median_price) * 100)
    message = (
        f"🔥 Score: {deal_score} | Confidence: {confidence}% | {car['make']} {car['model']} {car['year']}\n"
        f"💰 {car['price']}€ — saves {saving}€ ({saving_pct}% below median {median_price}€)\n"
        f"📍 {car['km']}km | {car['date_posted']}\n"
        f"🔗 {car['listing_url']}"
    )
    requests.post(DISCORD_WEBHOOK, json={"content": message})

def check_deal(car):
    global total_deals

    rows = cursor.execute("""
        SELECT price FROM listings
        WHERE make = ? AND model = ?
        AND year BETWEEN ? AND ?
        AND km BETWEEN ? AND ?
    """, (car['make'].title(), car['model'].title(), car['year'] - 2, car['year'] + 2,
          car['km'] - 50000, car['km'] + 50000)).fetchall()

    count = len(rows)
    if count < 5:
        return

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

    if car['price'] < median_price * 0.8 and saving > 1000 and final_score > 0:
        total_deals += 1

        # ── ADDED: store deal, send based on mode ──────────────────────────
        is_new_deal = cursor.execute(
            "INSERT OR IGNORE INTO deals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, DATE('now'))",
            (car['make'].title(), car['model'].title(), car['year'], car['km'],
             car['price'], round(median_price), final_score, round(final_confidence * 100),
             car['listing_url'])
        ).rowcount > 0
        conn.commit()

        if mode == 1:
            send_deal_alert(car, round(median_price), final_score, round(final_confidence * 100))
        elif mode == 2 and is_new_deal:
            send_deal_alert(car, round(median_price), final_score, round(final_confidence * 100))
        # ───────────────────────────────────────────────────────────────────

def insert_car(car):
    cursor.execute(
        "INSERT OR IGNORE INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (car['make'].title(), car['model'].title(), car['year'], car['km'],
         car['hp'], car['price'], car['listing_url'], car['date_posted'])
    )
    return cursor.rowcount > 0

def clear_discord():
    for i in range(10):
        requests.post(DISCORD_WEBHOOK, json={"content": "."})

# ── ADDED: mode selection ───────────────────────────────────────────────────
print("Select mode:")
print("  1 - Full scrape, send ALL deals (including already seen)")
print("  2 - Full scrape, send only NEW deals (first time seen)")
while True:
    try:
        mode = int(input("Enter 1 or 2: ").strip())
        if mode in (1, 2):
            break
        print("Please enter 1 or 2.")
    except ValueError:
        print("Please enter 1 or 2.")
# ───────────────────────────────────────────────────────────────────────────

# SCRAPER

page = 1
total_scraped = 0
total_inserted = 0

clear_discord()

while True:
    url = (
        f"https://reklama5.mk/Search?city=0&cat=24&q=&f31=&priceFrom=&priceTo="
        f"&f33_from=&f33_to=&f36_from=&f36_to=&f35=&f37=&f138=&f10016_from="
        f"&f10016_to=&f10042=&sell=0&sell=1&buy=0&buy=1&trade=0&trade=1"
        f"&includeOld=0&includeOld=1&includeNew=0&includeNew=1&cargoReady=0"
        f"&DDVIncluded=0&private=0&company=0&page={page}&SortByPrice=0&zz=1&pageView="
    )
    soup = BeautifulSoup(requests.get(url).text, "html.parser")

    titles_car = soup.find_all("a", attrs={"class": "SearchAdTitle"})
    car_desc_divs = soup.find_all("div", attrs={"class": "ad-desc-div"})
    prices = soup.find_all("span", attrs={"class": "search-ad-price"})

    if not titles_car:
        break

    for title, desc_div, price in zip(titles_car, car_desc_divs, prices):
        try:
            title_text = strip_prefix(title.text.strip())
            title_parts = title_text.split()
            make = title_parts[0].replace("-", "")
            make = make_map.get(make.upper(), make.title())

            # handle two-word makes
            if make in two_word_makes:
                model = title_parts[2]
            else:
                model = title_parts[1]
        except IndexError:
            continue

        if make.upper().replace("-", "").replace(" ", "") not in known_makes_upper:
            continue

        listing_url = "https://reklama5.mk" + title["href"]

        date_span = desc_div.find("span", style=lambda s: s and "float:right" in s.replace(" ", ""))
        listing_date = date_span.text.strip().replace("\n", " ") if date_span else None

        p = desc_div.find("p")
        if not p:
            continue
        try:
            specs = " ".join(p.stripped_strings)
            specs_parts = specs.split("•")
            year = int(specs_parts[0].strip())
            if year < 1990 or year > 2026:
                continue
            km = int(specs_parts[1].replace("km", "").replace(",", "").strip())
            hp = int(specs_parts[2].split("(")[1].replace("Hp)", "").strip())
        except:
            continue

        try:
            price_text = price.get_text(separator=" ", strip=True)
            value = int(price_text.replace("€", "").replace(".", "").strip())
            if value < 500 or value > 80000:
                continue
        except ValueError:
            continue

        car = {
            "make": make,
            "model": model,
            "year": year,
            "km": km,
            "hp": hp,
            "price": value,
            "listing_url": listing_url,
            "date_posted": listing_date
        }

        total_scraped += 1
        if insert_car(car):
            total_inserted += 1
        check_deal(car)

    print(f"Page {page} done")
    page += 1
    time.sleep(1)

conn.commit()
conn.close()
print(f"Scraping Done! Scraped: {total_scraped} | Inserted: {total_inserted} | Deals found: {total_deals}")