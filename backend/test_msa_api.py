#!/usr/bin/env python3
"""
Simple test script to verify MSA API endpoints work
Run this after starting the FastAPI server
"""
import requests
import json
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000/api/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer YOUR_TOKEN_HERE"  # Will be replaced by actual token
}

def test_columns_endpoint():
    """Test the /msa/columns endpoint"""
    print("\n" + "="*60)
    print("TEST 1: GET /msa/columns")
    print("="*60)
    
    try:
        url = f"{BASE_URL}/msa/columns"
        print(f"URL: {url}")
        
        response = requests.get(url, headers=HEADERS)
        print(f"Status Code: {response.status_code}")
        
        data = response.json()
        print(f"Response:\n{json.dumps(data, indent=2)}")
        
        if response.status_code == 200:
            columns = data.get('data', {}).get('columns', [])
            dates = data.get('data', {}).get('dates', [])
            print(f"\n✅ Success!")
            print(f"   - Columns: {len(columns)} items")
            print(f"   - Dates: {len(dates)} items")
            if dates:
                print(f"   - Sample dates: {dates[:3]}")
            return True
        else:
            print(f"❌ Failed with status {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

def test_without_auth():
    """Test endpoint without authentication (for debugging)"""
    print("\n" + "="*60)
    print("TEST 2: GET /msa/columns (without auth)")
    print("="*60)
    
    try:
        url = f"{BASE_URL}/msa/columns"
        print(f"URL: {url}")
        
        # Try without auth header
        response = requests.get(url)
        print(f"Status Code: {response.status_code}")
        
        data = response.json()
        print(f"Response:\n{json.dumps(data, indent=2)}")
        
        if response.status_code == 200:
            print(f"✅ Endpoint accessible without auth")
            return True
        elif response.status_code == 401:
            print(f"⚠️  Endpoint requires authentication (401)")
            return True  # Expected for protected endpoints
        else:
            print(f"❌ Unexpected status {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

if __name__ == "__main__":
    print(f"\n🚀 MSA API Test Suite")
    print(f"Base URL: {BASE_URL}")
    print(f"Timestamp: {datetime.now().isoformat()}")
    
    # Test without auth first (to see if authentication is blocking)
    test_without_auth()
    
    print("\n" + "="*60)
    print("INSTRUCTIONS:")
    print("="*60)
    print("1. Make sure FastAPI server is running on http://localhost:8000")
    print("2. If you have a valid JWT token, update HEADERS with it")
    print("3. Check backend logs for detailed error messages")
    print("4. Look for fallback date generation logs in backend")
    print("\nExpected Response Structure:")
    print({
        "success": True,
        "data": {
            "columns": ["COL1", "COL2", ...],
            "dates": ["2024-01-15", "2024-01-14", ...],
            "sample_count": 30
        },
        "message": "Retrieved X columns and Y dates"
    })
