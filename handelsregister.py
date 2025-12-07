#!/usr/bin/env python3
"""
bundesAPI/handelsregister is the command-line interface for the shared register of companies portal for the German federal states.
You can query, download, automate and much more, without using a web browser.
"""

import argparse
import tempfile
import mechanize
import re
import pathlib
import sys
from bs4 import BeautifulSoup
import urllib.parse
import datetime

# Dictionaries to map arguments to values
schlagwortOptionen = {
    "all": 1,
    "min": 2,
    "exact": 3
}

class HandelsRegister:
    def __init__(self, args):
        self.args = args
        self.browser = mechanize.Browser()

        self.browser.set_debug_http(args.debug)
        self.browser.set_debug_responses(args.debug)
        # self.browser.set_debug_redirects(True)

        self.browser.set_handle_robots(False)
        self.browser.set_handle_equiv(True)
        self.browser.set_handle_gzip(True)
        self.browser.set_handle_refresh(False)
        self.browser.set_handle_redirect(True)
        self.browser.set_handle_referer(True)

        self.browser.addheaders = [
            (
                "User-Agent",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ),
            (   "Accept-Language", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"   ),
            (   "Accept-Encoding", "gzip, deflate, br"    ),
            (
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            ),
            (   "Connection", "keep-alive"    ),
            (   "Sec-Fetch-Dest", "document" ),
            (   "Sec-Fetch-Mode", "navigate" ),
            (   "Sec-Fetch-Site", "same-origin" ),
            (   "Upgrade-Insecure-Requests", "1" ),
        ]
        
        self.cachedir = pathlib.Path(tempfile.gettempdir()) / "handelsregister_cache"
        self.cachedir.mkdir(parents=True, exist_ok=True)

    def open_startpage(self):
        self.browser.open("https://www.handelsregister.de", timeout=10)

    def companyname2cachename(self, companyname):
        return self.cachedir / companyname

    def search_company(self):
        cachename = self.companyname2cachename(self.args.schlagwoerter)
        if self.args.force==False and cachename.exists():
            with open(cachename, "r") as f:
                html = f.read()
                if not self.args.json:
                    print("return cached content for %s" % self.args.schlagwoerter)
        else:
            # TODO implement token bucket to abide by rate limit
            # Use an atomic counter: https://gist.github.com/benhoyt/8c8a8d62debe8e5aa5340373f9c509c7
            self.browser.select_form(name="naviForm")
            self.browser.form.new_control('hidden', 'naviForm:erweiterteSucheLink', {'value': 'naviForm:erweiterteSucheLink'})
            self.browser.form.new_control('hidden', 'target', {'value': 'erweiterteSucheLink'})
            response_search = self.browser.submit()

            if self.args.debug == True:
                print(self.browser.title())

            self.browser.select_form(name="form")
            
            # Use register number fields if available and parseable
            reg_parsed = False
            if self.args.register_number:
                match = re.search(r'(HRA|HRB|GnR|VR|PR)\s*(\d+)', self.args.register_number)
                if match:
                    reg_type = match.group(1)
                    reg_num = match.group(2)
                    try:
                        self.browser["form:registerArt_input"] = [reg_type]
                        self.browser["form:registerNummer"] = reg_num
                        self.browser["form:schlagwoerter"] = ""
                        reg_parsed = True
                    except Exception as e:
                        if self.args.debug:
                            print(f"Failed to set register number fields: {e}")
            
            if not reg_parsed:
                # Fallback to keywords
                 self.browser["form:schlagwoerter"] = self.args.schlagwoerter
                 
            so_id = schlagwortOptionen.get(self.args.schlagwortOptionen)
            self.browser["form:schlagwortOptionen"] = [str(so_id)]

            response_result = self.browser.submit()

            if self.args.debug == True:
                print(self.browser.title())

            html = response_result.read().decode("utf-8")
            with open(cachename, "w") as f:
                f.write(html)

            # TODO catch the situation if there's more than one company?
            # TODO get all documents attached to the exact company
            # TODO parse useful information out of the PDFs
        
        companies = get_companies_in_searchresults(html)
        return companies

    def get_documents(self, dk_id):
        self.browser.select_form(name="ergebnissForm")
        self.browser.form.new_control('hidden', 'javax.faces.source', {'value': dk_id})
        # Try full postback instead of partial AJAX to avoid complex state issues and get full page
        # self.browser.form.new_control('hidden', 'javax.faces.partial.event', {'value': 'click'})
        # self.browser.form.new_control('hidden', 'javax.faces.partial.execute', {'value': '@component'})
        # self.browser.form.new_control('hidden', 'javax.faces.partial.render', {'value': '@component'})
        self.browser.form.new_control('hidden', dk_id, {'value': dk_id})
        
        # Determine current ViewState if possible, mechanize usually handles it if we selected form
        # But we added controls, so it should be fine.
        
        try:
            response = self.browser.submit()
            content = response.read().decode('utf-8')
            return self.parse_documents(content)
        finally:
             # We might be on a different page now (documents page), so back might be needed 
             # to return to search results for next iteration.
             # If submission failed or redirected, history tracking in mechanize handles it.
            try:
                self.browser.back()
            except Exception as e:
                if self.args.debug:
                    print(f"Error going back: {e}")

    def parse_documents(self, html):
        if "session has expired" in html.lower() or "sitzung abgelaufen" in html.lower():
            print("[WARN] Session expired while fetching documents. Use a browser to download.")
            return []

        soup = BeautifulSoup(html, 'html.parser')
        docs = []
        
        # Strategy: Look for all text nodes that look like dates, then find nearby links or context
        # The tree structure usually puts the document name (with date) in a span/label
        
        # Regex for date: dd.mm.yyyy
        date_pattern = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
        
        # Broad search for any element containing a date
        # We limit to likely containers
        elements_with_dates = soup.find_all(string=date_pattern)
        
        for text_node in elements_with_dates:
            date_match = date_pattern.search(text_node)
            if not date_match:
                continue
            
            date_str = date_match.group(1)
            doc_date = None
            try:
                doc_date = datetime.datetime.strptime(date_str, "%d.%m.%Y")
            except:
                continue
                
            # Now try to find a link nearby.
            # In a tree, the link might be the element itself or a parent/sibling
            # Or the 'Download' button is separate.
            # If we can't find a direct download link, we might at least identify the document existence.
            # Currently we need 'pdf' link.
            
            # Use a placeholder link if we can't find it, or look harder.
            # Usually the tree node is click-able (commandLink).
            
            # Let's check parent 'a' tag
            node_parent = text_node.parent
            link = node_parent.find_parent('a') if node_parent else None
            
            if not link and node_parent:
                # Sibling?
                pass
            
            # Heuristic: If we found a date, it's likely a document record.
            # If we can't find a real PDF link (because it requires another click),
            # we can store a "virtual" link or similar specific ID.
            
            # For now, let's assume if it is inside an 'a' tag or clickable, we take it.
            # If no link, we skip for now (or store as 'No Link')
            
            pdf_link = link['href'] if link and 'href' in link.attrs else None
            # If link is '#' it is likely a JSF action -> not a direct PDF.
            if pdf_link == '#':
                pdf_link = link.get('id') # Store ID to indicate it's interactable
            
            docs.append({
                 'id': pdf_link or "unknown", 
                 'pdf': pdf_link, # Might be None
                 'date': doc_date,
                 'name': text_node.strip()
             })

        # Fallback: Table parsing (original logic) if above yielded nothing or mixed
        if not docs:
            tables = soup.find_all('table', role='grid')
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) < 3:
                         continue
                    
                    date_str = None
                    doc_date = None
                    
                    for cell in cells:
                        text = cell.text.strip()
                        match = date_pattern.search(text)
                        if match:
                            date_str = match.group(0)
                            try:
                                doc_date = datetime.datetime.strptime(date_str, "%d.%m.%Y")
                            except:
                                pass
                            break
                    
                    if not doc_date:
                        continue
                        
                    pdf_link = None
                    for cell in cells:
                        link = cell.find('a')
                        if link and ('href' in link.attrs):
                            pdf_link = link['href'] 
                            break
                    
                    if pdf_link:
                         docs.append({
                             'id': pdf_link, 
                             'pdf': pdf_link,
                             'date': doc_date
                         })

        # Deduplicate by date + id
        unique_docs = {}
        for d in docs:
            key = f"{d['date']}_{d['id']}"
            unique_docs[key] = d
        
        sorted_docs = sorted(unique_docs.values(), key=lambda x: x['date'], reverse=True)
        return sorted_docs

    def get_company(self, register_num):
        """
        Fetch a specific company by its register number and retrieve its documents.
        """
        # The register number often works as a search term
        # But picking specific fields is better.
        # We set self.args.register_number to ensure search_company uses the specific fields if possible.
        self.args.register_number = register_num
        self.args.schlagwoerter = register_num # Fallback if parsing fails
        
        companies = self.search_company()
        if self.args.debug:
            print(f"Found {len(companies)} companies. Searching match for '{register_num}'...")
            for c in companies:
                print(f" - {c.get('name')} ({c.get('register_num')})")
        
        target_company = None
        # 1. Try exact match
        for c in companies:
             if c.get('register_num') == register_num:
                 target_company = c
                 break
        
        # 2. Try normalized match (ignore spaces)
        if not target_company:
             clean_reg = register_num.replace(' ', '')
             for c in companies:
                 if c.get('register_num', '').replace(' ', '') == clean_reg:
                     target_company = c
                     break
        
        # 3. Try containment (if input is "HRB 12345" and result is "HRB 12345 B")
        # Only if we don't have an exact match
        if not target_company:
            for c in companies:
                # Check if result starts with the input (assuming input is the prefix part)
                if c.get('register_num', '').startswith(register_num):
                    target_company = c
                    break

        if self.args.withShareholdersLatest and target_company and target_company.get('_dk_id'):
            try:
                docs = self.get_documents(target_company['_dk_id'])
                # Only keep the last (latest) document if requested logic implies "only the last one"
                # Since we sort descending, [0] is the latest.
                if docs:
                    target_company['documents'] = [docs[0]]
                else:
                    target_company['documents'] = []
            except Exception as e:
                if self.args.debug:
                    print(f"Error fetching documents for {target_company.get('name')}: {e}")
        
        return target_company



def parse_result(result):
    cells = []
    for cellnum, cell in enumerate(result.find_all('td')):
        cells.append(cell.text.strip())
    d = {}
    d['court'] = cells[1]
    
    # Extract register number: HRB, HRA, VR, GnR followed by numbers (e.g. HRB 12345, VR 6789)
    # Also capture suffix letter if present (e.g. HRB 12345 B), but avoid matching start of words (e.g. " Formerly")
    reg_match = re.search(r'(HRA|HRB|GnR|VR|PR)\s*\d+(\s+[A-Z])?(?!\w)', d['court'])
    d['register_num'] = reg_match.group(0) if reg_match else None

    d['name'] = cells[2]
    d['state'] = cells[3]
    d['status'] = cells[4].strip()  # Original value for backward compatibility
    d['statusCurrent'] = cells[4].strip().upper().replace(' ', '_')  # Transformed value

    # Ensure consistent register number suffixes (e.g. ' B' for Berlin HRB, ' HB' for Bremen) which might be implicit
    if d['register_num']:
        suffix_map = {
            'Berlin': {'HRB': ' B'},
            'Bremen': {'HRA': ' HB', 'HRB': ' HB', 'GnR': ' HB', 'VR': ' HB', 'PR': ' HB'}
        }
        reg_type = d['register_num'].split()[0]
        suffix = suffix_map.get(d['state'], {}).get(reg_type)
        if suffix and not d['register_num'].endswith(suffix):
            d['register_num'] += suffix
            
    d['documents'] = [] 
    d['_dk_id'] = None
    
    # Try to extract the DK (Dokumente) link ID
    # We need the 'result' object (the tr) passed to this function
    if len(result.find_all('td')) > 5:
        td_docs = result.find_all('td')[5]
        # Look for span with text 'DK'
        dk_span = td_docs.find('span', string=re.compile(r'DK'))
        if dk_span:
            link = dk_span.find_parent('a')
            if link:
                d['_dk_id'] = link.get('id')

    d['history'] = []
    hist_start = 8

    for i in range(hist_start, len(cells), 3):
        if i + 1 >= len(cells):
            break
        if "Branches" in cells[i] or "Niederlassungen" in cells[i]:
            break
        d['history'].append((cells[i], cells[i+1])) # (name, location)

    return d

def pr_company_info(c):
    for tag in ('name', 'court', 'register_num', 'district', 'state', 'statusCurrent'):
        print('%s: %s' % (tag, c.get(tag, '-')))
    print('history:')
    for name, loc in c.get('history'):
        print(name, loc)
    print('documents:')
    for doc in c.get('documents', []):
        print(f"  {doc.get('date')} - {doc.get('id')}")

def get_companies_in_searchresults(html):
    soup = BeautifulSoup(html, 'html.parser')
    grid = soup.find('table', role='grid')
  
    results = []
    for result in grid.find_all('tr'):
        a = result.get('data-ri')
        if a is not None:
            index = int(a)

            d = parse_result(result)
            results.append(d)
    return results

def parse_args():
    parser = argparse.ArgumentParser(description='A handelsregister CLI')
    parser.add_argument(
                          "-d",
                          "--debug",
                          help="Enable debug mode and activate logging",
                          action="store_true"
                        )
    parser.add_argument(
                          "-f",
                          "--force",
                          help="Force a fresh pull and skip the cache",
                          action="store_true"
                        )
    parser.add_argument(
                          "-s",
                          "--schlagwoerter",
                          help="Search for the provided keywords",
                          required=False,
                          default=None
                        )
    parser.add_argument(
                          "-so",
                          "--schlagwortOptionen",
                          help="Keyword options: all=contain all keywords; min=contain at least one keyword; exact=contain the exact company name.",
                          choices=["all", "min", "exact"],
                          default="all"
                        )
    parser.add_argument(
                          "-r",
                          "--register_number",
                          help="Search for a specific register number (e.g. HRB 44343 B) and fetch documents",
                          default=None
                        )
    parser.add_argument(
                          "-j",
                          "--json",
                          help="Return response as JSON",
                          action="store_true"
                        )
    parser.add_argument(
                          "-wsl",
                          "--withShareholdersLatest",
                          help="Fetch the latest shareholder list document for the company",
                          action="store_true"
                        )
    args = parser.parse_args()


    # Enable debugging if wanted
    if args.debug == True:
        import logging
        logger = logging.getLogger("mechanize")
        logger.addHandler(logging.StreamHandler(sys.stdout))
        logger.setLevel(logging.DEBUG)

    return args

if __name__ == "__main__":
    import json
    
    # Custom JSON encoder for datetime
    class DateTimeEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, datetime.datetime):
                return o.isoformat()
            return super().default(o)
            
    args = parse_args()
    
    if not args.schlagwoerter and not args.register_number:
        print("Error: Either -s/--schlagwoerter or -r/--register_number must be provided.")
        sys.exit(1)
        
    h = HandelsRegister(args)
    h.open_startpage()
    
    if args.register_number:
        company = h.get_company(args.register_number)
        if company:
            if args.json:
                company_out = {k: v for k, v in company.items() if not k.startswith('_')}
                print(json.dumps(company_out, cls=DateTimeEncoder))
            else:
                pr_company_info(company)
        else:
            if not args.json:
                print(f"Company with register number {args.register_number} not found.")
    else:
        companies = h.search_company()
        if companies is not None:
            if args.json:
                companies_out = [{k: v for k, v in c.items() if not k.startswith('_')} for c in companies]
                print(json.dumps(companies_out, cls=DateTimeEncoder))
            else:
                for c in companies:
                    pr_company_info(c)
