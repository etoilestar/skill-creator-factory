import os
import requests
import json

def run(payload=None, config=None) -> dict:
    # Hardcoded values as per human feedback
    api_url = "http://172.18.127.67:9090/upload_pdf"
    user_id = "123"
    task_type = "skill"
    
    # Extract pdf_path from payload (assuming it's passed in payload['pdf_path'])
    pdf_path = payload.get('pdf_path') if payload else None
    
    if not pdf_path:
        return {
            'status': 'failed',
            'output_md_path': None,
            'message': 'Missing pdf_path in payload'
        }
    
    # Validate file exists
    if not os.path.exists(pdf_path):
        return {
            'status': 'failed',
            'output_md_path': None,
            'message': f'PDF file not found: {pdf_path}'
        }
    
    try:
        # Prepare the request data
        with open(pdf_path, 'rb') as f:
            files = {'file': f}
            data = {'user_id': user_id, 'task_type': task_type}
            
            # Send POST request to the API
            response = requests.post(api_url, files=files, data=data)
            
        # Check if the request was successful
        if response.status_code != 200:
            return {
                'status': 'failed',
                'output_md_path': None,
                'message': f'API request failed with status code {response.status_code}: {response.text}'
            }
        
        # Parse response
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            response_data = {'content': response.text}
        
        # Assuming the API returns a JSON with a 'content' field
        md_content = response_data.get('content', '')
        
        # Write output to a file
        output_md_path = 'output.md'
        with open(output_md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        return {
            'status': 'success',
            'output_md_path': output_md_path,
            'message': 'PDF converted to Markdown successfully'
        }
        
    except Exception as e:
        return {
            'status': 'failed',
            'output_md_path': None,
            'message': f'An error occurred during conversion: {str(e)}'
        }


def pdf_to_md(payload=None) -> dict:
    return run(payload)


def pdf_to_md_mineru(payload: dict | None = None, config: dict | None = None) -> dict:
    """Public entrypoint for this generated tool; delegates to run()."""
    return run(dict(payload or {}), config)