"""
Salesforce Data Cloud Search Index API Client Package
"""

from .salesforce_api import (
    authenticate_soap,
    get_salesforce_credentials,
    invoke_prompt,
    clean_html_response,
    retrieve_metadata_via_api,
    resolve_prompt_template_name_from_id,
    SearchIndexAPI,
)
from .excel_io import create_analysis_sheet_with_prompts

__all__ = [
    'authenticate_soap',
    'get_salesforce_credentials',
    'invoke_prompt',
    'clean_html_response',
    'retrieve_metadata_via_api',
    'resolve_prompt_template_name_from_id',
    'SearchIndexAPI',
    'create_analysis_sheet_with_prompts',
]



