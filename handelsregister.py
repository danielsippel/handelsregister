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
import base64

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
        self.browser.set_handle_refresh(True)
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
            docs = self.parse_documents(content)
            
            current_viewstate = None
            
            if not docs:
                try:
                    # Step 1: Expand root 0_0
                    xml_content = self.expand_documents_tree("0_0", current_viewstate)
                    if xml_content:
                         # Extract new ViewState
                         current_viewstate = self.extract_viewstate_from_partial_response(xml_content)
                         tree_html = self.extract_html_from_partial_response(xml_content)
                         
                         soup = BeautifulSoup(tree_html, 'html.parser')
                         nodes_to_expand = []
                         
                         for li in soup.find_all('li'):
                             txt = li.get_text()
                             # Expand all folder-like nodes that are not leaves
                             # 'data-nodetype="list"' usually denotes a folder
                             if li.get('data-nodetype') == 'list':
                                 rid = li.get('data-rowkey')
                                 if rid: nodes_to_expand.append((rid, txt))
                        
                         # Step 2: Expand relevant nodes
                         for nid, category_name in nodes_to_expand:
                             xml_content_2 = self.expand_documents_tree(nid, current_viewstate)
                             if xml_content_2:
                                 current_viewstate = self.extract_viewstate_from_partial_response(xml_content_2)
                                 html_2 = self.extract_html_from_partial_response(xml_content_2)
                                 docs += self.parse_documents(html_2, default_type=category_name)
                                 
                except Exception as e:
                    if self.args.debug:
                        print(f"Debug: Failed to expand tree: {e}")
            
            if self.args.withShareholdersLatest and docs:
                 s_docs = [d for d in docs if d.get('name') and 'gesellschafter' in d['name'].lower()]
                 if not s_docs:
                      s_docs = [d for d in docs if d.get('type') and 'GESELLSCHAFTER' in d['type']]
                 
                 if s_docs:
                      latest = s_docs[0]
                      if self.args.debug:
                           print(f"Found latest shareholder list (internal): {latest['name']}")
                      
                      pdf_content = self.download_pdf(latest['pdf'])
                      if not pdf_content and latest.get('rowkey'):
                           if self.args.debug: print(f"Downloading via rowkey {latest['rowkey']}")
                           pdf_content = self.download_pdf_via_rowkey(latest['rowkey'])
                           
                      if pdf_content:
                           latest['pdf_base64'] = base64.b64encode(pdf_content).decode('utf-8')

            return docs
        finally:
             # We might be on a different page now (documents page), so back might be needed 
             # to return to search results for next iteration.
             # If submission failed or redirected, history tracking in mechanize handles it.
            try:
                self.browser.back()
            except Exception as e:
                if self.args.debug:
                    print(f"Error going back: {e}")

    def expand_documents_tree(self, node_id="0_0", viewstate=None):
        # Select the tree form
        try:
            self.browser.select_form(name="dk_form")
        except:
            if self.args.debug:
                print("Debug: dk_form not found")
            return None

        # Update ViewState if we have a newer one from previous AJAX requests
        if viewstate:
            try:
                self.browser.form.set_value(viewstate, name='javax.faces.ViewState')
            except Exception as e:
                 # It might be that the name is different or control not found, but standard JSF uses this name
                 if self.args.debug: print(f"Debug: Could not set ViewState: {e}")

        # Add parameters to expand the node
        # Note: We must be careful not to add duplicate controls if they persist (mechanize shouldn't persist new_control across select_form if we back() correctly)
        self.browser.form.new_control('hidden', 'javax.faces.partial.ajax', {'value': 'true'})
        self.browser.form.new_control('hidden', 'javax.faces.source', {'value': 'dk_form:dktree'})
        self.browser.form.new_control('hidden', 'javax.faces.partial.execute', {'value': 'dk_form:dktree'})
        self.browser.form.new_control('hidden', 'javax.faces.partial.render', {'value': 'dk_form:dktree'})
        self.browser.form.new_control('hidden', 'dk_form:dktree_expandNode', {'value': node_id})
        self.browser.form.new_control('hidden', 'dk_form:dktree_scrollState', {'value': '0,0'})
        
        # Submit the AJAX request
        response = self.browser.submit()
        content = response.read().decode('utf-8')
        
        # IMPORTANT: Restore browser state to the HTML page so we can interact with the form again
        self.browser.back()
        
        return content

    def extract_viewstate_from_partial_response(self, xml_content):
        import re
        # Look for <update id="...javax.faces.ViewState...">...</update>
        # The ID is usually something like j_id1:javax.faces.ViewState:0
        
        match = re.search(r'<update id="[^"]*javax\.faces\.ViewState[^"]*"><!\[CDATA\[(.*?)\]\]></update>', xml_content, re.DOTALL)
        if match:
            return match.group(1)
        return None


    def extract_html_from_partial_response(self, xml_content):
        try:
            import re
            # Remove encoding declaration
            if '<?xml' in xml_content:
                xml_content = xml_content[xml_content.find('?>')+2:]
            
            # Target the specific update for the tree
            # <update id="dk_form:dktree"><![CDATA[...]]></update>
            # The ID usually matches exactly dk_form:dktree or contains it
            
            match = re.search(r'<update id="[^"]*dktree[^"]*"><!\[CDATA\[(.*?)\]\]></update>', xml_content, re.DOTALL)
            if match:
                return match.group(1)
            
            # Fallback if no CDATA or different format
            match = re.search(r'<update id="[^"]*dktree[^"]*">(.*?)</update>', xml_content, re.DOTALL)
            if match:
                return match.group(1)
                
        except Exception as e:
            if self.args.debug:
                print(f"Debug: Error parsing partial response: {e}")
        
        return ""

    def normalize_type(self, type_string):
        if not type_string: return None
        # Replace non alphanum (preserving german chars is tricky with just \w in some regex engines, 
        # but let's assume we want to keep them or just replace symbols)
        # Let's simple replace known separators
        t = type_string.replace(' / ', '_').replace(' - ', '_').replace(' ', '_').replace('/', '_')
        # Remove any other non-word characters except underscores (optional, but safer)
        # t = re.sub(r'[^\w\d_]', '', t) # This might strip German chars depending on locale
        
        t = t.upper()
        # Merge underscores
        t = re.sub(r'_+', '_', t)
        return t.strip('_')

    def parse_documents(self, html, default_type=None):
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
            
            name = text_node.strip()
            rowkey = None
            if not pdf_link:
                 # Try to find rowkey in 'li' parent
                 # hierarchy: text -> span(label) -> span(content) -> li
                 curr = node_parent
                 for _ in range(4): # check up to 4 levels up
                      if curr and curr.name == 'li' and curr.has_attr('data-rowkey'):
                           rowkey = curr['data-rowkey']
                           break
                      curr = curr.parent if curr else None

            if self.args.debug and not pdf_link and "gesellschafter" in name.lower():
                 print(f"DEBUG: Found Gesellschafter but no PDF link. Node parent: {node_parent}")
                 if rowkey:
                      print(f"DEBUG: Found rowkey: {rowkey}")
                 elif node_parent and node_parent.parent:
                      print(f"DEBUG: Node grandparent: {node_parent.parent}")

            docs.append({
                 'id': pdf_link or name, 
                 'pdf': pdf_link, # Might be None
                 'rowkey': rowkey,
                 'date': doc_date,
                 'name': name,
                 'typeString': default_type,
                 'type': self.normalize_type(default_type)
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
                             'date': doc_date,
                             'name': date_str, # Fallback name
                             'typeString': default_type,
                             'type': self.normalize_type(default_type)
                         })

        # Deduplicate by date + id
        unique_docs = {}
        for d in docs:
            key = f"{d['date']}_{d['id']}"
            unique_docs[key] = d
        
        sorted_docs = sorted(unique_docs.values(), key=lambda x: x['date'], reverse=True)
        return sorted_docs

    def download_pdf(self, url):
        if not url or url.startswith('#') or (':' in url and not url.startswith('http')):
             return None
        try:
            resp = self.browser.open(url)
            return resp.read()
        except Exception as e:
            if self.args.debug:
                 print(f"Failed to download PDF: {e}")
            return None

    def download_pdf_via_rowkey(self, rowkey):
        if not rowkey: return None
        try:
            self.browser.select_form(name="dk_form")
            
            self.browser.form.new_control('hidden', 'javax.faces.partial.ajax', {'value': 'true'})
            self.browser.form.new_control('hidden', 'javax.faces.source', {'value': 'dk_form:dktree'})
            self.browser.form.new_control('hidden', 'javax.faces.partial.execute', {'value': 'dk_form:dktree'})
            # Trying specifically to update the whole form to see download links
            self.browser.form.new_control('hidden', 'javax.faces.partial.render', {'value': 'dk_form'}) 
            self.browser.form.new_control('hidden', 'dk_form:dktree_selection', {'value': rowkey})
            self.browser.form.new_control('hidden', 'javax.faces.behavior.event', {'value': 'select'})
            
            response = self.browser.submit()

            xml_content = response.read().decode('utf-8')
            
            new_viewstate = self.extract_viewstate_from_partial_response(xml_content)
            
            self.browser.back()
            
            import re
            btn_match = re.search(r'<button[^>]*name="([^"]+)"[^>]*>.*?Download.*?</button>', xml_content, re.DOTALL | re.IGNORECASE)
            
            if not btn_match:
                 btn_match = re.search(r'<input[^>]*name="([^"]+)"[^>]*value="Download"[^>]*>', xml_content, re.DOTALL | re.IGNORECASE)
                 
            if not btn_match and self.args.debug:
                 print(f"DEBUG: No Download button found in AJAX response. Content length: {len(xml_content)}")
                 print(f"DEBUG: content snippet: {xml_content[:1000]}")

            if btn_match:
                 btn_name = btn_match.group(1)
                 if self.args.debug: 
                     print(f"DEBUG: Found download button in AJAX response: {btn_name}")
                 
                 if self.args.debug: print("DEBUG: Reloading page to sync state...")
                 self.browser.open(self.browser.geturl())
                 
                 self.browser.select_form(name="dk_form")
                 
                 try:
                     self.browser.form.find_control(btn_name) # Verification
                     if self.args.debug: print(f"DEBUG: Button {btn_name} found in reloaded form.")
                     
                     dl_response = self.browser.submit(name=btn_name)
                 except Exception as e:
                     if self.args.debug: print(f"DEBUG: Could not find/click button {btn_name} in reloaded page: {e}")
                     # Fallback: force it like before, but on the fresh form
                     try:
                        self.browser.form.new_control('hidden', btn_name, {'value': ''})
                        dl_response = self.browser.submit()
                     except Exception as e2:
                        print(f"WARN: Failed to forcedly submit download button: {e2}", file=sys.stderr)
                        return None

                 ct = dl_response.info().get_content_type()
                 
                 # Relaxed content type check
                 if 'pdf' in ct or 'octet-stream' in ct or 'zip' in ct:
                      content = dl_response.read()
                      
                      # Handle ZIP response (Handelsregister sometimes zips files)
                      if content.startswith(b'PK'):
                           if self.args.debug: print("DEBUG: Response is a ZIP file. Extracting PDF...")
                           import zipfile
                           import io
                           try:
                               with zipfile.ZipFile(io.BytesIO(content)) as z:
                                   # Find first PDF
                                   for name in z.namelist():
                                       if name.lower().endswith('.pdf'):
                                           if self.args.debug: print(f"DEBUG: Extracted {name} from ZIP.")
                                           content = z.read(name)
                                           break
                           except Exception as e:
                               print(f"WARN: Failed to extract ZIP: {e}", file=sys.stderr)
                               # If we can't unzip, we can't give a PDF.
                               return None
                      
                      self.browser.back()
                      return content

                 # If we are here, it's not a known binary type; check for "Please wait" page
                 try:
                      content = dl_response.read()
                      
                      if b"Bitte warten" in content:
                           if b"ui-hidden-container" in content and b"Bitte warten" in content:
                               if self.args.debug: print("DEBUG: 'Bitte warten' text found but likely hidden. Ignoring 'Please wait' warning.", file=sys.stderr)
                           else:
                               print("WARN: Enountered 'Please wait' page. This page requires JavaScript to proceed, which is not supported.", file=sys.stderr)
                               if self.args.debug:
                                    print(f"DEBUG: Response URL: {dl_response.geturl()}")
                 except: 
                      pass
                        
                 self.browser.back()
                 return None
            
            # Fallback to link search if button not found (unlikely given XML analysis)
            links = re.findall(r'href=["\']([^"\']+)["\']', xml_content)
            
            for link in links:
                 # Skip JS, hash, resources
                 if link.startswith('#') or link.startswith('javascript'): continue
                 if 'javax.faces.resource' in link: continue
                 
                 # Prioritize likely download links
                 # Often they are just relative links like 'documents/...'
                 full_link = urllib.parse.urljoin(self.browser.geturl(), link)
                 
                 # Optimization: avoid fetching unrelated pages
                 # But we don't know the exact pattern.
                 
                 # Try to download
                 try:
                      res = self.browser.open(full_link)
                      # Check content type?
                      ct = res.info().get_content_type()
                      if ct == 'application/pdf':
                           return res.read()
                      # If not PDF, maybe skip?
                 except:
                      pass
            
            return None

        except Exception as e:
            if self.args.debug:
                 print(f"Error downloading via rowkey: {e}")
            return None

    def get_company(self, register_num, company_name=None):
        """
        Fetch a specific company by its register number (and optionally company name) and retrieve its documents.
        NOTE: register_number is NOT unique! Use company_name for more reliable identification.
        If company_name is provided, it is used to disambiguate between multiple companies
        with the same register number at different courts.
        """
        # Strategy: Extract a core search term from company_name (if provided) to cast a wider net in search
        # Then filter strictly by exact company_name + register_number
        # This is necessary because:
        # 1. Searching by full company name doesn't work well in Handelsregister
        # 2. Searching by register_number alone returns only first page of results (might be 100+ companies)
        # 3. We need a unique combination to find the right company
        
        search_term = None
        if company_name:
            # Extract core search term (first significant word, usually the company's distinctive name)
            # Remove common suffixes like "GmbH", "AG", etc.
            import re
            clean_name = re.sub(r'\s+(GmbH|AG|UG|KG|OHG|e\.V\.|eG|mbH|SE|Co\.|&|und)\s*', ' ', company_name, flags=re.IGNORECASE)
            words = clean_name.strip().split()
            # Use first word as core search term (usually the distinctive part)
            search_term = words[0] if words else company_name
            
            if self.args.debug:
                print(f"Searching for '{search_term}' (extracted from '{company_name}')")
                print(f"Will filter by exact name '{company_name}' and register '{register_num}'...")
        else:
            # Fallback: search by register number
            search_term = register_num
            if self.args.debug:
                print(f"Warning: Searching by register_number only (not unique!). Consider providing company_name.")
        
        # Use the search term for lookup
        self.args.schlagwoerter = search_term
        # Don't set register_number in args to avoid confusing the search
        self.args.register_number = None
        
        companies = self.search_company()
        if self.args.debug:
            print(f"Found {len(companies)} companies in search results...")
            for c in companies:
                print(f" - {c.get('name')} ({c.get('register_num')})")
        
        # If company_name is provided, filter by name first
        if company_name:
            if self.args.debug:
                print(f"Filtering by company name: '{company_name}'")
            
            # Try exact match first (case-sensitive)
            name_filtered = [c for c in companies if c.get('name') == company_name]
            
            # If no exact match, try case-insensitive
            if not name_filtered:
                company_name_lower = company_name.lower()
                name_filtered = [c for c in companies if c.get('name', '').lower() == company_name_lower]
            
            # If still no match, try substring match
            if not name_filtered:
                company_name_lower = company_name.lower()
                name_filtered = [c for c in companies if company_name_lower in c.get('name', '').lower()]
            
            if name_filtered:
                companies = name_filtered
                if self.args.debug:
                    print(f"After name filtering: {len(companies)} companies remaining")
            else:
                if self.args.debug:
                    print(f"Warning: No companies found matching name '{company_name}'")
        
        target_company = None
        
        # Filter by register_number
        matches_by_register = []
        clean_reg = register_num.replace(' ', '')
        
        for c in companies:
            c_reg = c.get('register_num', '')
            # Try exact match
            if c_reg == register_num:
                matches_by_register.append(c)
            # Try normalized match (ignore spaces)
            elif c_reg.replace(' ', '') == clean_reg:
                matches_by_register.append(c)
            # Try containment (if input is "HRB 12345" and result is "HRB 12345 B")
            elif c_reg.startswith(register_num):
                matches_by_register.append(c)
        
        if self.args.debug:
            print(f"Found {len(matches_by_register)} companies matching register_number filter")
        
        # If we have company_name, filter further by name similarity
        if company_name and matches_by_register:
            if len(matches_by_register) == 1:
                target_company = matches_by_register[0]
                if self.args.debug:
                    print(f"Single match found: {target_company.get('name')}")
            else:
                # Multiple matches - use company name to disambiguate
                clean_name = company_name.lower().strip()
                for c in matches_by_register:
                    c_name = c.get('name', '').lower().strip()
                    if clean_name in c_name or c_name in clean_name:
                        target_company = c
                        if self.args.debug:
                            print(f"Matched by company name: {c.get('name')}")
                        break
                
                if not target_company:
                    # No name match - take first match and warn
                    target_company = matches_by_register[0]
                    if self.args.debug:
                        print(f"Warning: Multiple matches, using first: {target_company.get('name')}")
        elif matches_by_register:
            # No company_name provided, just take first match by register_number
            target_company = matches_by_register[0]
            if len(matches_by_register) > 1 and self.args.debug:
                print(f"Warning: Multiple companies with register_number '{register_num}'. Using first match.")

        if target_company and target_company.get('_dk_id'):
            if self.args.debug:
                print(f"Debug: Found _dk_id {target_company.get('_dk_id')}, fetching documents...")
            try:
                docs = self.get_documents(target_company['_dk_id'])
                target_company['documents'] = docs

                if self.args.withShareholdersLatest:
                     target_company['documentShareholdersLatest'] = None
                     if docs:
                         for d in docs:
                              if d.get('pdf_base64'):
                                   target_company['documentShareholdersLatest'] = d['pdf_base64']
                                   break


            except Exception as e:
                if self.args.debug:
                    print(f"Error fetching documents for {target_company.get('name')}: {e}")
        elif self.args.withShareholdersLatest:
            if self.args.debug:
                 print(f"Debug: No _dk_id found for company (target found: {bool(target_company)})")
        
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

    # Extract federalState and city from court string
    # Format usually: "State   District court City (Suffix) Type Number" or similar
    # e.g. "Berlin   Amtsgericht Berlin (Charlottenburg) HRB 138434"
    # e.g. "Bayern   Amtsgericht München HRB 231893"
    
    court_clean = d['court'].strip()
    # Pattern: ^(State)\s+(District court|Amtsgericht)\s+(City.*?)(\s+(HRA|HRB|GnR|VR|PR)\s+\d+)?$
    # Relaxed pattern to capture the parts
    
    # 1. State is at the start, until multiple spaces or "District court"/"Amtsgericht"
    # Actually, cells[3] already contains the state ("Berlin", "Bayern"). Let's use that as federalState.
    d['federalState'] = d['state']

    # 2. City is the part after "District court"/"Amtsgericht" and before the register type
    # We can use regex to cut out the middle part
    city_match = re.search(r'(?:District court|Amtsgericht)\s+(.*?)\s+(?:HRA|HRB|GnR|VR|PR)', court_clean)
    if city_match:
        d['city'] = city_match.group(1).strip()
    else:
        # Fallback: take everything after court type if no reg type at end (unlikely for valid entries)
        city_match_fallback = re.search(r'(?:District court|Amtsgericht)\s+(.*)', court_clean)
        d['city'] = city_match_fallback.group(1).strip() if city_match_fallback else None

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
                # print(f"Debug: Found DK ID: {d['_dk_id']}")
        # else:
            # print("Debug: No DK span found in column 5")

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
        t = doc.get('typeString', '-')
        print(f"  {doc.get('date')} - {t} - {doc.get('name')}")

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
                          help="Search for a specific register number (e.g. HRB 44343 B) and fetch documents. Must be used together with --company_name.",
                          default=None
                        )
    parser.add_argument(
                          "-cn",
                          "--company_name",
                          help="Company name to disambiguate between multiple companies with the same register number. Required when using --register_number.",
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
    
    # Validate that company_name is provided when register_number is used
    if args.register_number and not args.company_name:
        print("Error: --company_name is required when using --register_number.")
        print("Reason: Register numbers are not unique across different courts.")
        print("Example: 'HRB 8391' exists at multiple courts in Hessen.")
        sys.exit(1)
        
    h = HandelsRegister(args)
    h.open_startpage()
    
    if args.register_number:
        company = h.get_company(args.register_number, args.company_name)
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
        # Only company name/keywords provided - do search
        companies = h.search_company()
        if companies is not None:
            if args.json:
                companies_out = [{k: v for k, v in c.items() if not k.startswith('_')} for c in companies]
                print(json.dumps(companies_out, cls=DateTimeEncoder))
            else:
                for c in companies:
                    pr_company_info(c)
