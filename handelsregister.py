#!/usr/bin/env python3
"""
handelsregister.py - Refactored to use Playwright for robust JavaScript support.
"""

import argparse
import sys
import re
import json
import datetime
import pathlib
import tempfile
import time
from bs4 import BeautifulSoup

# Try importing Playwright
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Error: playwright is not installed. Please run: pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)

# Dictionaries to map arguments to values
schlagwortOptionen = {
    "all": 1,
    "min": 2,
    "exact": 3
}

class HandelsRegister:
    def __init__(self, args):
        self.args = args
        self.playwright = sync_playwright().start()
        # Headless by default unless debug is on
        self.browser = self.playwright.chromium.launch(headless=not args.debug)
        self.context = self.browser.new_context(
             user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
             locale="de-DE",
             viewport={'width': 1280, 'height': 1024}
        )
        self.page = self.context.new_page()
        
        self.cachedir = pathlib.Path(tempfile.gettempdir()) / "handelsregister_cache"
        self.cachedir.mkdir(parents=True, exist_ok=True)

    def close(self):
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def companyname2cachename(self, companyname):
        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', companyname)
        return self.cachedir / f"{safe_name}.html"

    def open_startpage(self):
        try:
            self.page.goto("https://www.handelsregister.de", timeout=60000)
            # Handle cookie consent if visible? ignoring for now as headless usually works without
        except Exception as e:
            if self.args.debug:
                print(f"Error opening startpage: {e}")
            raise

    def search_company(self):
        cachename = self.companyname2cachename(self.args.schlagwoerter or self.args.register_number or "search")
        
        # Check cache if not forced
        if False and self.args.force == False and cachename.exists(): # Disabled cache for now to ensure fresh JS execution
            with open(cachename, "r") as f:
                html = f.read()
                if not self.args.json:
                    print("return cached content")
                return get_companies_in_searchresults(html)

        # 1. Navigate to Extended Search
        # The link is usually id="naviForm:erweiterteSucheLink"
        try:
             # Wait for the link to be visible and click
             self.page.wait_for_selector("#naviForm\\:erweiterteSucheLink", timeout=10000)
             self.page.click("#naviForm\\:erweiterteSucheLink")
        except Exception as e:
             if self.args.debug:
                 print(f"Could not find extended search link, checking if we are already there or redirected. Error: {e}")

        self.page.wait_for_load_state("networkidle")
        
        # 2. Fill Form
        # Determine if we use register number fields
        reg_parsed = False
        if self.args.register_number:
            match = re.search(r'(HRA|HRB|GnR|VR|PR)\s*(\d+)', self.args.register_number)
            if match:
                reg_type = match.group(1)
                reg_num = match.group(2)
                try:
                    # Type format: Check if it's a select or input?
                    # In standard JSF primefaces, it might be unique.
                    # Try to fill registerNummer
                    if self.page.is_visible("#form\\:registerNummer"):
                        self.page.fill("#form\\:registerNummer", reg_num)
                    
                    # Try select registerArt
                    # Since parsing selectOneMenu in PrimeFaces is hard with standard select_option if hidden,
                    # we try standard first.
                    try:
                        self.page.select_option("#form\\:registerArt_input", label=reg_type)
                    except:
                        # Fallback: try value match
                        try:
                           self.page.select_option("#form\\:registerArt_input", value=reg_type)
                        except:
                           pass

                    # Clear keywords just in case
                    self.page.fill("#form\\:schlagwoerter", "")
                    reg_parsed = True
                except Exception as e:
                    if self.args.debug:
                        print(f"Failed to fill register number fields: {e}")

        if not reg_parsed:
            self.page.fill("#form\\:schlagwoerter", self.args.schlagwoerter)
            # Options
            so_id = schlagwortOptionen.get(self.args.schlagwortOptionen, 1)
            # Try to click the corresponding radio/checkbox if we can find it.
            # Assuming defaults for now.

        # 3. Click Search
        # Button often has id ending in :kostenpflichtigabrufen or similar, or just text "Suchen"
        try:
            self.page.click("button:has-text('Suchen')")
        except:
             # Fallback to id
             self.page.click("[id$=':kostenpflichtigabrufen']")

        # 4. Wait for results
        try:
            self.page.wait_for_selector("table[role='grid']", timeout=30000)
        except:
            if self.args.debug:
                print("No results table found.")
            return []

        html = self.page.content()
        # Cache?
        # with open(cachename, "w") as f:
        #     f.write(html)
            
        return get_companies_in_searchresults(html)

    def get_documents(self, dk_id):
        # dk_id is the HTML id of the element to click
        if not dk_id:
            return []
            
        # Selectors with colons need escaping
        selector = f"#{dk_id.replace(':', '\\\\:')}"
        
        try:
            # Click and wait for network idle (AJAX or page load)
            self.page.click(selector)
            self.page.wait_for_load_state("networkidle")
            
            # Additional wait if needed for the tree creation
            time.sleep(1) 
            
            html = self.page.content()
            return self.parse_documents(html)
        except Exception as e:
            if self.args.debug:
                print(f"Error fetching documents: {e}")
            return []

    def parse_documents(self, html):
        if "session has expired" in html.lower() or "sitzung abgelaufen" in html.lower():
            if self.args.debug:
                print("[WARN] Session expired.")
            return []

        soup = BeautifulSoup(html, 'html.parser')
        docs = []
        
        # Use simple heuristic: find all links that look like download actions or have dates
        # Or reused logic looking for dates
        date_pattern = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
        
        # 1. Look for rows in a tree table or standard table
        # Primefaces usually uses table[role='tree'] or similar
        # But let's look for text nodes with dates again
        elements_with_dates = soup.find_all(string=date_pattern)
        
        for text_node in elements_with_dates:
            date_match = date_pattern.search(text_node)
            if not date_match:
                continue
            
            date_str = date_match.group(1)
            try:
                doc_date = datetime.datetime.strptime(date_str, "%d.%m.%Y")
            except:
                continue
                
            # Find closest link
            link = text_node.find_parent('a')
            if not link:
                # maybe sibling?
                pass
            
            # If we are in the document view, we might find actual PDF links or "AD" (Aktueller Abdruck) links
            # The structure varies.
            
            # Construct a doc object
            # We use text_node as name
            docs.append({
                 'date': doc_date,
                 'name': text_node.strip(),
                 'id': link.get('id') if link else 'unknown',
                 'pdf': link.get('href') if link else None
            })

        # Deduplicate
        unique_docs = {}
        for d in docs:
            key = f"{d['date']}_{d['name']}"
            unique_docs[key] = d
            
        sorted_docs = sorted(unique_docs.values(), key=lambda x: x['date'], reverse=True)
        return sorted_docs

    def get_company(self, register_num):
        self.args.register_number = register_num
        self.args.schlagwoerter = register_num
        
        companies = self.search_company()
        
        target_company = None
        # Logic to find best match
        for c in companies:
             if c.get('register_num') == register_num:
                 target_company = c
                 break
        
        if not target_company and companies:
             # Fallback logic
             target_company = companies[0] # simplified for now

        if self.args.withShareholdersLatest and target_company and target_company.get('_dk_id'):
            docs = self.get_documents(target_company['_dk_id'])
            target_company['documents'] = [docs[0]] if docs else []
            
        return target_company

# --- Parsing Helpers ---

def parse_result(result):
    cells = []
    for cellnum, cell in enumerate(result.find_all('td')):
        cells.append(cell.text.strip())
    
    d = {}
    if len(cells) < 5: 
        return d # Should not happen if data is valid
        
    d['court'] = cells[1]
    
    reg_match = re.search(r'(HRA|HRB|GnR|VR|PR)\s*\d+(\s+[A-Z])?(?!\w)', d['court'])
    d['register_num'] = reg_match.group(0) if reg_match else None

    d['name'] = cells[2]
    d['state'] = cells[3]
    d['status'] = cells[4].strip() 
    d['statusCurrent'] = cells[4].strip().upper().replace(' ', '_')

    # City/FederalState Parsing
    d['federalState'] = d['state']
    court_clean = d['court'].strip()
    
    city_match = re.search(r'(?:District court|Amtsgericht)\s+(.*?)\s+(?:HRA|HRB|GnR|VR|PR)', court_clean)
    if city_match:
        d['city'] = city_match.group(1).strip()
    else:
        city_match_fallback = re.search(r'(?:District court|Amtsgericht)\s+(.*)', court_clean)
        d['city'] = city_match_fallback.group(1).strip() if city_match_fallback else None

    # Suffix check
    if d['register_num']:
        suffix_map = {
            'Berlin': {'HRB': ' B'},
            'Bremen': {'HRA': ' HB', 'HRB': ' HB', 'GnR': ' HB', 'VR': ' HB', 'PR': ' HB'}
        }
        reg_type = d['register_num'].split()[0]
        suffix = suffix_map.get(d['state'], {}).get(reg_type)
        if suffix and not d['register_num'].endswith(suffix):
            d['register_num'] += suffix
            
    # Links
    d['documents'] = []
    d['_dk_id'] = None
    
    # Check for DK link
    if len(result.find_all('td')) > 5:
        td_docs = result.find_all('td')[5]
        dk_span = td_docs.find('span', string=re.compile(r'DK'))
        if dk_span:
            link = dk_span.find_parent('a')
            if link:
                d['_dk_id'] = link.get('id')

    # History
    d['history'] = []
    # Simplified history parsing
    
    return d

def get_companies_in_searchresults(html):
    soup = BeautifulSoup(html, 'html.parser')
    grid = soup.find('table', role='grid')
    if not grid:
        return []
        
    results = []
    for result in grid.find_all('tr'):
        if result.get('data-ri') is not None:
            d = parse_result(result)
            if d: results.append(d)
    return results

def parse_args():
    parser = argparse.ArgumentParser(description='A handelsregister CLI (Playwright Version)')
    parser.add_argument("-d", "--debug", help="Enable debug mode", action="store_true")
    parser.add_argument("-f", "--force", help="Force fresh pull", action="store_true")
    parser.add_argument("-s", "--schlagwoerter", help="Search keywords", default=None)
    parser.add_argument("-so", "--schlagwortOptionen", help="Options", default="all")
    parser.add_argument("-r", "--register_number", help="Register number", default=None)
    parser.add_argument("-j", "--json", help="JSON Output", action="store_true")
    parser.add_argument("-wsl", "--withShareholdersLatest", help="Fetch latest shareholder list", action="store_true")
    return parser.parse_args()

# --- Main ---

if __name__ == "__main__":
    class DateTimeEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, datetime.datetime):
                return o.isoformat()
            return super().default(o)
            
    args = parse_args()
    if not args.schlagwoerter and not args.register_number:
        # If no arguments, maybe just exit or print help?
        # But sometimes script is called with just one.
        pass

    h = HandelsRegister(args)
    
    try:
        h.open_startpage()
        
        if args.register_number:
            company = h.get_company(args.register_number)
            if company:
                if args.json:
                    company_out = {k: v for k, v in company.items() if not k.startswith('_')}
                    print(json.dumps(company_out, cls=DateTimeEncoder))
                else:
                    print(company)
            else:
                if args.json: print("{}")
                else: print("Not found")
        else:
            companies = h.search_company()
            if args.json:
                 companies_out = [{k: v for k, v in c.items() if not k.startswith('_')} for c in companies]
                 print(json.dumps(companies_out, cls=DateTimeEncoder))
            else:
                 print(f"Found {len(companies)} companies.")
    except Exception as e:
        sys.stderr.write(str(e))
        if args.debug:
            raise
    finally:
        h.close()
