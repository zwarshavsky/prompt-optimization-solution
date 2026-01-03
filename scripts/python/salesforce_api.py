"""
Unified Salesforce programmatic client.

This single module consolidates:
- Auth (SOAP login / token handling)
- Prompt APIs (invoke prompt template, retrieve prompt metadata)
- Search Index APIs (list/get/wait/validate/copy/update index settings)

Playwright UI automation remains separate (see playwright_scripts.py).
"""

import json
import sys
import re
import time
import requests
import xml.etree.ElementTree as ET
import yaml
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Helper function for immediate output flushing
def log_print(*args, **kwargs):
    """Print with immediate flush for real-time terminal output"""
    print(*args, **kwargs, flush=True)

# ============================================================================
# Auth + Prompt helpers (from utils.py)
# ============================================================================

def authenticate_soap(username: str, password: str, instance_url: str = None):
    """Authenticate to Salesforce via SOAP; returns (instance_url, access_token)."""
    log_print(f"   🔐 Authenticating to Salesforce via SOAP...")
    if instance_url and 'login.salesforce.com' not in instance_url:
        domain = instance_url.replace('https://', '').replace('http://', '').split('/')[0]
        soap_url = f"https://{domain}/services/Soap/u/58.0"
        log_print(f"   📍 Using custom domain: {domain}")
    else:
        soap_url = "https://login.salesforce.com/services/Soap/u/58.0"
        log_print(f"   📍 Using standard login endpoint")
    
    soap_envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:partner.soap.sforce.com">
   <soapenv:Header/>
   <soapenv:Body>
      <urn:login>
         <urn:username>{username}</urn:username>
         <urn:password>{password}</urn:password>
      </urn:login>
   </soapenv:Body>
</soapenv:Envelope>"""

    headers = {'Content-Type': 'text/xml; charset=UTF-8', 'SOAPAction': 'login'}
    
    try:
        log_print(f"   ⏳ Sending SOAP login request (timeout: 30s)...")
        response = requests.post(soap_url, data=soap_envelope, headers=headers, timeout=30)
        log_print(f"   ✅ SOAP response received (status: {response.status_code})")
        response.raise_for_status()
        root = ET.fromstring(response.text)
        namespaces = {'soapenv': 'http://schemas.xmlsoap.org/soap/envelope/', 'urn': 'urn:partner.soap.sforce.com'}
        result = root.find('.//urn:result', namespaces)
        if result is None:
            fault = root.find('.//soapenv:Fault', namespaces)
            if fault is not None:
                fault_string = fault.find('soapenv:faultstring', namespaces)
                error_msg = fault_string.text if fault_string is not None else "SOAP login failed"
                raise Exception(f"SOAP Login Failed: {error_msg}")
            raise Exception("Could not parse SOAP response")
        
        session_id = result.find('urn:sessionId', namespaces)
        server_url = result.find('urn:serverUrl', namespaces)
        if session_id is None or server_url is None:
            raise Exception("Could not extract sessionId or serverUrl from SOAP response")
        
        access_token = session_id.text
        instance_url_actual = server_url.text.split('/services')[0]
        log_print(f"   ✅ Authentication successful: {instance_url_actual}")
        return instance_url_actual.rstrip('/'), access_token
    except requests.exceptions.RequestException as e:
        log_print(f"   ❌ SOAP authentication request failed: {e}")
        raise Exception(f"SOAP authentication request failed: {e}")
    except ET.ParseError as e:
        log_print(f"   ❌ Failed to parse SOAP response: {e}")
        raise Exception(f"Failed to parse SOAP response: {e}")


def get_salesforce_credentials(username: str = None, password: str = None, instance_url: str = None):
    """Get Salesforce instance URL and access token using SOAP authentication."""
    if username and password:
        log_print(f"   🔑 Using provided credentials for user: {username}")
        return authenticate_soap(username, password, instance_url)
    log_print(f"   🔑 Loading credentials from YAML config...")
    try:
        yaml_path = Path(__file__).parent.parent.parent / "inputs" / "prompt_optimization_input.yaml"
        log_print(f"   📄 YAML path: {yaml_path}")
        if yaml_path.exists():
            with open(yaml_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            salesforce_config = config.get('configuration', {}).get('salesforce', {})
            yaml_username = salesforce_config.get('username')
            yaml_password = salesforce_config.get('password')
            yaml_instance_url = salesforce_config.get('instanceUrl')
            if yaml_username and yaml_password:
                log_print(f"   ✅ Found credentials in YAML for user: {yaml_username}")
                return authenticate_soap(yaml_username, yaml_password, yaml_instance_url)
            else:
                log_print("❌ Error: YAML config found but username/password not configured")
                log_print(f"   YAML path: {yaml_path}")
        else:
            log_print(f"❌ Error: YAML config file not found: {yaml_path}")
    except Exception as e:
        log_print(f"❌ Error reading YAML config: {e}")
    log_print("❌ Error: Could not obtain Salesforce credentials")
    sys.exit(1)


def retrieve_metadata_via_api(instance_url: str, access_token: str, metadata_type: str, metadata_name: str) -> str:
    """Retrieve Salesforce metadata using the SOAP Metadata API."""
    soap_url = f"{instance_url}/services/Soap/m/65.0"
    soap_envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:met="http://soap.sforce.com/2006/04/metadata">
   <soapenv:Header>
      <met:SessionHeader>
         <met:sessionId>{access_token}</met:sessionId>
      </met:SessionHeader>
   </soapenv:Header>
   <soapenv:Body>
      <met:readMetadata>
         <met:type>{metadata_type}</met:type>
         <met:fullNames>{metadata_name}</met:fullNames>
      </met:readMetadata>
   </soapenv:Body>
</soapenv:Envelope>"""

    headers = {'Content-Type': 'text/xml; charset=UTF-8', 'SOAPAction': 'readMetadata'}
    try:
        response = requests.post(soap_url, data=soap_envelope, headers=headers, timeout=60)
        response.raise_for_status()
        root = ET.fromstring(response.text)
        namespaces = {'soapenv': 'http://schemas.xmlsoap.org/soap/envelope/', 'met': 'http://soap.sforce.com/2006/04/metadata'}
        fault = root.find('.//soapenv:Fault', namespaces)
        if fault is not None:
            fault_string = fault.find('soapenv:faultstring', namespaces)
            error_msg = fault_string.text if fault_string is not None else "SOAP fault"
            raise Exception(f"Metadata API SOAP fault: {error_msg}")
        result = root.find('.//met:result', namespaces)
        if result is not None:
            return ET.tostring(result, encoding='unicode')
        return None
    except requests.exceptions.RequestException as e:
        raise Exception(f"Metadata API request failed: {e}")
    except ET.ParseError as e:
        raise Exception(f"Failed to parse SOAP response: {e}")


def resolve_prompt_template_name_from_id(instance_url: str, access_token: str, prompt_template_id: str) -> str:
    """Placeholder for resolving prompt template ID to DeveloperName."""
    return None


def clean_html_response(text):
    """Remove HTML markup and make response human-friendly."""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    return text.strip()


def invoke_prompt(instance_url, access_token, question, prompt_name, max_retries=3, model_used=None, models_list=None):
    """Invoke prompt template via REST API with retry logic and logging."""
    if models_list and len(models_list) > 0:
        models_to_try = models_list
    elif model_used:
        models_to_try = [model_used]
    else:
        models_to_try = ["Unknown"]
    
    session = requests.Session()
    
    for model_idx, current_model in enumerate(models_to_try):
        if model_idx > 0:
            log_print(f"      🔄 Trying fallback model {model_idx + 1}/{len(models_to_try)}: {current_model}")
        url = f"{instance_url}/services/data/v65.0/actions/custom/generatePromptResponse/{prompt_name}"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        payload = {"inputs": [{"Input:Question": question}]}
        
        for attempt in range(max_retries):
            if attempt > 0:
                log_print(f"      ⏳ Retry attempt {attempt + 1}/{max_retries}...")
            try:
                response = session.post(url, headers=headers, json=payload, timeout=60)
                try:
                    result = response.json()
                except:
                    response_text = response.text
                    if attempt < max_retries - 1:
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    return (f"API Error: {response.status_code}, JSON parse failed, response: '{response_text[:200]}', url='{url}'", current_model)
                
                if response.status_code == 200:
                    if result and len(result) > 0 and result[0].get('isSuccess', False):
                        prompt_response = result[0].get('outputValues', {}).get('promptResponse', '')
                        return (clean_html_response(prompt_response), current_model)
                    
                    errors = result[0].get('errors', []) if result and len(result) > 0 else []
                    if errors:
                        error_messages = []
                        for e in errors:
                            if isinstance(e, dict):
                                error_messages.append(e.get('message', str(e)))
                            else:
                                error_messages.append(str(e))
                        error_msg = ', '.join(error_messages) if error_messages else 'Unknown error'
                        error_msg_lower = error_msg.lower()
                        is_provider_rate_limit = (
                            'provider rate limit' in error_msg_lower or 
                            ('provider' in error_msg_lower and 'rate limit' in error_msg_lower) or
                            ('remaining=0' in error_msg and 'limit=' in error_msg and 'errors;minute' not in error_msg)
                        )
                        is_org_rate_limit = 'rate limit' in error_msg_lower and not is_provider_rate_limit
                        if is_provider_rate_limit:
                            if attempt < max_retries - 1:
                                wait_time = 1.0 * (2 ** attempt)
                                time.sleep(wait_time)
                                continue
                            elif model_idx < len(models_to_try) - 1:
                                break
                            else:
                                return (f"Error: Provider rate limit on all models - {error_msg[:200]}", current_model)
                        if is_org_rate_limit and attempt < max_retries - 1:
                            reset_match = re.search(r'reset=(\\d+)', error_msg)
                            if reset_match:
                                wait_time = int(reset_match.group(1)) + 1
                            else:
                                wait_time = 1.0 * (2 ** attempt)
                            time.sleep(wait_time)
                            continue
                        return (f"Error: {error_msg[:200]}", current_model)
                    return (f"Error: Unknown error - isSuccess was False", current_model)
                else:
                    errors = result[0].get('errors', []) if result and len(result) > 0 else []
                    error_messages = []
                    for e in errors:
                        if isinstance(e, dict):
                            error_messages.append(e.get('message', str(e)))
                        else:
                            error_messages.append(str(e))
                    error_msg = ', '.join(error_messages) if error_messages else 'Unknown error'
                    error_msg_lower = error_msg.lower()
                    is_provider_rate_limit = (
                        'provider rate limit' in error_msg_lower or 
                        ('provider' in error_msg_lower and 'rate limit' in error_msg_lower) or
                        ('remaining=0' in error_msg and 'limit=' in error_msg and 'errors;minute' not in error_msg)
                    )
                    is_org_rate_limit = 'rate limit' in error_msg_lower and not is_provider_rate_limit
                    if is_provider_rate_limit:
                        if attempt < max_retries - 1:
                            wait_time = 1.0 * (2 ** attempt)
                            time.sleep(wait_time)
                            continue
                        elif model_idx < len(models_to_try) - 1:
                            break
                        else:
                            return (f"API Error: {response.status_code}, Provider rate limit on all models - {error_msg[:200]}", current_model)
                    if is_org_rate_limit and attempt < max_retries - 1:
                        reset_match = re.search(r'reset=(\\d+)', error_msg)
                        if reset_match:
                            wait_time = int(reset_match.group(1)) + 1
                        else:
                            wait_time = 1.0 * (2 ** attempt)
                        time.sleep(wait_time)
                        continue
                    return (f"API Error: {response.status_code}, {error_msg[:200]}", current_model)
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = 1.0 * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
                else:
                    return (f"Error: Request failed after retries - {str(e)[:200]}", current_model)
        continue
    return ("Error: All models exhausted or failed", models_to_try[-1] if models_to_try else "Unknown")


# ============================================================================
# Search Index API (from search_index_api.py)
# ============================================================================

class SearchIndexAPI:
    """Client for Salesforce Data Cloud Search Index API operations."""
    
    def __init__(self, instance_url: Optional[str] = None, access_token: Optional[str] = None):
        if instance_url and access_token:
            self.instance_url = instance_url.rstrip('/')
            self.access_token = access_token
        else:
            self.instance_url, self.access_token = get_salesforce_credentials()
        
        self.api_version = 'v65.0'
        self.base_url = f"{self.instance_url}/services/data/{self.api_version}/ssot/search-index"
        
        self.session = requests.Session()
        retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers = {'Authorization': f'Bearer {self.access_token}', 'Content-Type': 'application/json'}
        
        try:
            if method == 'GET':
                response = self.session.get(url, headers=headers, timeout=60)
            elif method == 'POST':
                response = self.session.post(url, headers=headers, json=data, timeout=120)
            elif method == 'PATCH':
                response = self.session.patch(url, headers=headers, json=data, timeout=60)
            elif method == 'DELETE':
                response = self.session.delete(url, headers=headers, timeout=60)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            response.raise_for_status()
            return response.json() if response.text else {}
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            raise Exception(error_msg) from e
        except requests.exceptions.RequestException as e:
            raise Exception(f"Request failed: {e}") from e
    
    def list_indexes(self) -> List[Dict[str, Any]]:
        return self._make_request('GET', '')
    
    def get_index(self, index_id: str) -> Dict[str, Any]:
        return self._make_request('GET', f'/{index_id}')
    
    def create_index(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._make_request('POST', '', payload)
    
    def update_index(self, index_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._make_request('PATCH', f'/{index_id}', payload)
    
    def delete_index(self, index_id: str) -> Dict[str, Any]:
        return self._make_request('DELETE', f'/{index_id}')
    
    def wait_for_ready(self, index_id: str, timeout_seconds: int = 900, poll_interval: int = 10) -> Dict[str, Any]:
        start_time = time.time()
        while True:
            idx = self.get_index(index_id)
            status = idx.get('status')
            if status == 'READY':
                return idx
            if status == 'FAILED':
                raise Exception(f"Index {index_id} failed")
            if time.time() - start_time > timeout_seconds:
                raise Exception(f"Timed out waiting for index {index_id} to be READY")
            time.sleep(poll_interval)
    
    def validate_index(self, index_id: str) -> Dict[str, Any]:
        idx = self.get_index(index_id)
        status = idx.get('status')
        if status != 'READY':
            return {"ok": False, "reason": f"Status is {status}"}
        try:
            chunk_count = self._get_dmo_count("Einstein_Eve_Chunk__dlm")
            vector_count = self._get_dmo_count("Einstein_Eve_Vector__dlm")
        except Exception as e:
            return {"ok": False, "reason": f"DMO count error: {e}"}
        if chunk_count == 0 or vector_count == 0:
            return {"ok": False, "reason": f"DMO counts too low (chunks={chunk_count}, vectors={vector_count})"}
        return {"ok": True, "status": status, "chunks": chunk_count, "vectors": vector_count}
    
    def _get_dmo_count(self, dmo_name: str) -> int:
        url = f"{self.instance_url}/services/data/v65.0/query?q=SELECT+count()+FROM+{dmo_name}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = self.session.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data.get('totalSize', 0)
    
    def copy_index_with_embedding_model(self, source_index_id: str, new_index_name: str,
                                        embedding_model: str, max_token_limit: Optional[int] = None,
                                        enable_image_processing: bool = False) -> Dict[str, Any]:
        source_index = self.get_index(source_index_id)
        payload = source_index
        payload.pop('id', None)
        payload['name'] = new_index_name
        vec_cfg = payload.get('vectorEmbeddingConfiguration', {})
        if vec_cfg:
            model = vec_cfg.get('embeddingModel', {})
            model['model'] = embedding_model
            if max_token_limit is not None:
                model.setdefault('userValues', {})['max_token_limit'] = max_token_limit
            vec_cfg['embeddingModel'] = model
            payload['vectorEmbeddingConfiguration'] = vec_cfg
        transforms = payload.get('transformConfigurations', [])
        for t in transforms:
            if t.get('type') == 'IMAGE':
                t['enabled'] = bool(enable_image_processing)
        payload['transformConfigurations'] = transforms
        return self.create_index(payload)


__all__ = [
    "authenticate_soap",
    "get_salesforce_credentials",
    "invoke_prompt",
    "clean_html_response",
    "retrieve_metadata_via_api",
    "resolve_prompt_template_name_from_id",
    "SearchIndexAPI",
]


