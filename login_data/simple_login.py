#!/usr/bin/env python3
"""
Simple Nuvama Login Helper

This script handles the two-step Nuvama login process:
1. Submit UserID and click proceed
2. Submit password and TOTP (auto-generated using pyotp)

Usage:
    python simple_login.py --username your_username --password your_password --totp_secret your_totp_secret
    
    # Or set environment variable
    export TOTP_SECRET=your_totp_secret
    python simple_login.py --username your_username --password your_password
"""

import os
import json
import time
import argparse
import webbrowser
import re
from urllib.parse import urlparse, parse_qs
import requests
from requests.sessions import Session
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import pyotp
from datetime import datetime

class SimpleNuvamaLogin:
    def __init__(self, api_key="iBe07GpBTEbbhg", headless=False, totp_secret=None):
        self.api_key = api_key
        self.base_url = f"https://www.nuvamawealth.com/api-connect/login?api_key={api_key}"
        self.session = Session()
        self.driver = None
        self.wait = None
        self.headless = headless
        self.totp_secret = totp_secret
        
        # TOTP secrets for different users (replace with your actual secrets)
        self.totp_secrets = {
            "user1": "YOUR_TOTP_SECRET_KEY_1",
            "user2": "YOUR_TOTP_SECRET_KEY_2",
            # Add more users and their TOTP secrets as needed
        }
        
        # Setup session headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
    
    def setup_driver(self):
        """Setup Chrome WebDriver with Authenticator extension"""
        chrome_options = Options()
        
        if self.headless:
            chrome_options.add_argument("--headless")
        
        # Add Chrome options
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        
        # Disable notifications and popups
        prefs = {
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_settings.popups": 0
        }
        chrome_options.add_experimental_option("prefs", prefs)
        
        try:
            self.driver = webdriver.Chrome(options=chrome_options)
            self.wait = WebDriverWait(self.driver, 20)
            print("✅ Chrome WebDriver initialized successfully")
            return True
        except Exception as e:
            print(f"❌ Failed to initialize WebDriver: {e}")
            return False
    
    def submit_userid_and_proceed(self, username):
        """Step 1: Submit UserID and click proceed"""
        try:
            print(f"🔄 Step 1: Submitting UserID: {username}")
            
            # Navigate to login page
            print(f"🌐 Navigating to: {self.base_url}")
            self.driver.get(self.base_url)
            time.sleep(3)
            
            # Find and fill UserID field
            user_id_field = self.wait.until(
                EC.presence_of_element_located((By.ID, "userID"))
            )
            user_id_field.clear()
            user_id_field.send_keys(username)
            print(f"✅ UserID filled: {username}")
            
            # Find and click proceed button
            proceed_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:contains('Proceed')",
                "button:contains('Submit')",
                "button:contains('Next')"
            ]
            
            proceed_button = None
            for selector in proceed_selectors:
                try:
                    if "contains" in selector:
                        # Handle text-based selector
                        buttons = self.driver.find_elements(By.TAG_NAME, "button")
                        for btn in buttons:
                            btn_text = btn.text.lower()
                            if any(word in btn_text for word in ['proceed', 'submit', 'next']):
                                proceed_button = btn
                                break
                    else:
                        proceed_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    
                    if proceed_button:
                        break
                except NoSuchElementException:
                    continue
            
            if proceed_button:
                proceed_button.click()
                print("✅ Proceed button clicked for UserID step")
                time.sleep(4)  # Wait for page transition
                return True
            else:
                print("❌ Proceed button not found")
                return False
                
        except TimeoutException:
            print("❌ UserID field not found within timeout")
            return False
        except Exception as e:
            print(f"❌ Error in UserID step: {e}")
            return False
    
    def submit_password_and_totp(self, password, username):
        """Step 2: Submit password and TOTP"""
        try:
            print("� Step 2: Submitting password and TOTP")
            
            # Wait for password field to appear
            password_field = self.wait.until(
                EC.presence_of_element_located((By.ID, "password"))
            )
            password_field.clear()
            password_field.send_keys(password)
            print("✅ Password filled")
            
            # Find TOTP fields
            totp_fields = []
            for i in range(7):  # totp-0 to totp-6
                try:
                    totp_field = self.driver.find_element(By.ID, f"totp-{i}")
                    totp_fields.append(totp_field)
                except NoSuchElementException:
                    pass
            
            print(f"✅ Found {len(totp_fields)} TOTP fields")
            
            if totp_fields:
                # Generate TOTP using pyotp
                totp_code = self.generate_totp(username)
                
                if totp_code:
                    # Fill TOTP fields
                    for i, digit in enumerate(totp_code):
                        if i < len(totp_fields):
                            totp_fields[i].clear()
                            totp_fields[i].send_keys(digit)
                    print(f"✅ TOTP filled: {totp_code}")
                else:
                    print("❌ Could not generate TOTP")
                    return False
            
            # Find and click final proceed button
            proceed_button = self.find_proceed_button()
            if proceed_button:
                proceed_button.click()
                print("✅ Final proceed button clicked")
                time.sleep(5)  # Wait for redirect
                return True
            else:
                print("❌ Final proceed button not found")
                return False
                
        except TimeoutException:
            print("❌ Password field not found within timeout")
            return False
        except Exception as e:
            print(f"❌ Error in password/TOTP step: {e}")
            return False
    
    def generate_totp(self, username):
        """Generate TOTP code using pyotp"""
        try:
            print("📱 Generating TOTP using pyotp...")
            
            # Get TOTP secret for the user
            totp_secret = None
            
            # Priority 1: Use secret passed during initialization
            if self.totp_secret:
                totp_secret = self.totp_secret
                print("✅ Using TOTP secret from initialization")
            
            # Priority 2: Use secret from user mapping
            elif username in self.totp_secrets:
                totp_secret = self.totp_secrets[username]
                print(f"✅ Using TOTP secret for user: {username}")
            
            # Priority 3: Try to read from environment variable
            elif os.getenv(f"TOTP_SECRET_{username.upper()}"):
                totp_secret = os.getenv(f"TOTP_SECRET_{username.upper()}")
                print(f"✅ Using TOTP secret from environment variable: TOTP_SECRET_{username.upper()}")
            
            # Priority 4: Try generic environment variable
            elif os.getenv("TOTP_SECRET"):
                totp_secret = os.getenv("TOTP_SECRET")
                print("✅ Using TOTP secret from generic environment variable: TOTP_SECRET")
            
            if not totp_secret or totp_secret.startswith("YOUR_TOTP_SECRET"):
                print("❌ No valid TOTP secret found!")
                print("Please provide TOTP secret using one of these methods:")
                print("1. Pass --totp_secret argument")
                print("2. Set environment variable TOTP_SECRET")
                print(f"3. Set environment variable TOTP_SECRET_{username.upper()}")
                print("4. Update totp_secrets dictionary in the script")
                return None
            
            # Generate TOTP code
            totp = pyotp.TOTP(totp_secret)
            totp_code = totp.now()
            
            print(f"✅ TOTP generated successfully: {totp_code}")
            return totp_code
            
        except Exception as e:
            print(f"❌ Error generating TOTP: {e}")
            return None
    
    def find_proceed_button(self):
        """Find the final proceed/login button"""
        proceed_selectors = [
            "button:contains('Proceed')",
            "button:contains('Login')",
            "button:contains('Submit')",
            "button[type='submit']",
            "input[type='submit']"
        ]
        
        for selector in proceed_selectors:
            try:
                if "contains" in selector:
                    buttons = self.driver.find_elements(By.TAG_NAME, "button")
                    for btn in buttons:
                        btn_text = btn.text.lower()
                        if any(word in btn_text for word in ['proceed', 'login', 'submit']):
                            return btn
                else:
                    return self.driver.find_element(By.CSS_SELECTOR, selector)
            except NoSuchElementException:
                continue
        
        return None
    
    def extract_auth_token(self, timeout=30):
        """Extract auth token from redirect URL"""
        print("🔍 Waiting for redirect and extracting auth token...")
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                current_url = self.driver.current_url
                
                # Check if URL has changed (successful login)
                if "login" not in current_url.lower():
                    print(f"✅ Redirect detected: {current_url}")
                    
                    # Parse URL for token
                    parsed_url = urlparse(current_url)
                    query_params = parse_qs(parsed_url.query)
                    
                    # Look for common token parameter names
                    token_keys = ['request_id', 'authToken', 'access_token', 'auth_token', 'apikey']
                    
                    for key in token_keys:
                        if key in query_params:
                            token = query_params[key][0]
                            print(f"🎉 Auth token extracted: {token}")
                            return token
                    
                    # If no token in URL, check for other success indicators
                    print("⚠️ Redirect successful but no token found in URL")
                    print(f"Final URL: {current_url}")
                    return current_url
                
                time.sleep(2)
                
            except Exception as e:
                print(f"⚠️ Error checking for redirect: {e}")
                time.sleep(2)
        
        print("⏰ Timeout waiting for redirect")
        return None
    
    def save_token(self, token, username):
        """Save token to file"""
        try:
            token_data = {
                "username": username,
                "token": token,
                "timestamp": time.time(),
                "api_key": self.api_key
            }
            
            with open("nuvama_token_simple.json", "w") as f:
                json.dump(token_data, f, indent=2)
            
            print(f"💾 Token saved to nuvama_token_simple.json")
            return True
        except Exception as e:
            print(f"❌ Failed to save token: {e}")
            return False
    
    def cleanup(self):
        """Clean up resources"""
        if self.driver:
            self.driver.quit()
            print("🧹 Browser closed")
    
    def login(self, username, password):
        """Main login process with two-step authentication"""
        print(f"🚀 Starting Nuvama login for: {username}")
        
        # Setup browser
        if not self.setup_driver():
            return False
        
        try:
            # Step 1: Submit UserID and proceed
            if not self.submit_userid_and_proceed(username):
                return False
            
            # Step 2: Submit password and TOTP
            if not self.submit_password_and_totp(password, username):
                return False
            
            # Step 3: Extract auth token
            token = self.extract_auth_token()
            
            if token:
                self.save_token(token, username)
                print("🎉 Login completed successfully!")
                return token
            else:
                print("❌ Failed to extract auth token")
                return False
                
        except Exception as e:
            print(f"❌ Login failed: {e}")
            return False
        finally:
            if not self.headless:
                input("\nPress Enter to close browser...")
            self.cleanup()

def main():
    parser = argparse.ArgumentParser(description="Simple Nuvama Two-Step Login with pyotp")
    parser.add_argument("--username", required=True, help="Nuvama username")
    parser.add_argument("--password", required=True, help="Nuvama password")
    parser.add_argument("--totp_secret", help="TOTP secret key for generating OTP")
    parser.add_argument("--api_key", default="iBe07GpBTEbbhg", help="API key for Nuvama")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    
    args = parser.parse_args()
    
    # Create login instance
    login_helper = SimpleNuvamaLogin(
        api_key=args.api_key,
        headless=args.headless,
        totp_secret=args.totp_secret
    )
    
    # Perform login
    success = login_helper.login(args.username, args.password)
    
    if success:
        print("\n✅ Login completed!")
    else:
        print("\n❌ Login failed!")

if __name__ == "__main__":
    main()
