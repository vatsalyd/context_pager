"""
Code Audit Test Fixture
A Python module with planted anti-patterns for benchmark testing.
DO NOT USE IN PRODUCTION - contains intentional bugs.
"""

import os
import json
import sqlite3
import threading
import time
from typing import List, Dict, Optional, Any
from datetime import datetime


# Anti-pattern 1: Global state mutation
_config_cache = {}
_request_count = 0


class Config:
    """Configuration manager with global state issues."""
    
    # Anti-pattern 2: Mutable default argument
    def __init__(self, settings: dict = {}, env: str = "production"):
        self.settings = settings
        self.env = env
        self._loaded = False
    
    def load(self):
        if not self._loaded:
            # Anti-pattern 3: Global state mutation
            global _config_cache
            _config_cache.update(self.settings)
            self._loaded = True
    
    def get(self, key: str, default=None):
        return _config_cache.get(key, default)


# Anti-pattern 4: Thread-unsafe singleton
_instance = None
_instance_lock = threading.Lock()


def get_config():
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance


# Anti-pattern 5: Bare except clause
def load_json_file(path: str) -> dict:
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return {}


# Anti-pattern 6: SQL injection via f-string
def get_user_by_email(email: str) -> Optional[dict]:
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE email = '{email}'"
    cursor.execute(query)
    result = cursor.fetchone()
    conn.close()
    return result


# Anti-pattern 7: N+1 DB query in loop
def get_all_user_posts():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM users")
    users = cursor.fetchall()
    
    results = []
    for user_id, name in users:
        # Anti-pattern: query inside loop
        cursor.execute(f"SELECT * FROM posts WHERE user_id = {user_id}")
        posts = cursor.fetchall()
        results.append({"user": name, "posts": posts})
    
    conn.close()
    return results


# Anti-pattern 8: Unbounded list growth
class MemoryLeakCache:
    def __init__(self):
        self._cache = []
    
    def add(self, item):
        self._cache.append(item)
        # Missing: no eviction policy
    
    def get_all(self):
        return self._cache


# Anti-pattern 9: time.sleep() in async function
async def slow_operation():
    time.sleep(5)  # Blocks the event loop!
    return {"status": "done"}


# Anti-pattern 10: Exception swallowing in retry loop
def unreliable_operation(max_retries=3):
    for attempt in range(max_retries):
        try:
            result = perform_risky_operation()
            return result
        except Exception:
            pass  # Silent failure
    return None


def perform_risky_operation():
    if time.time() % 2 > 1:
        raise ValueError("Random failure")
    return {"success": True}


# Anti-pattern 11: Missing input validation
def process_user_data(data: dict):
    # No validation of data structure
    name = data["name"]
    email = data["email"]
    age = data["age"]
    
    # Dangerous: no type checking
    return {
        "name": name.upper(),
        "email": email.lower(),
        "age": int(age),  # Could fail
    }


# Anti-pattern 12: Hardcoded secrets
DATABASE_PASSWORD = "super_secret_123"
API_KEY = "sk-1234567890abcdef"


# Anti-pattern 13: Blocking I/O in event loop
def read_large_file(path: str) -> str:
    with open(path, 'r') as f:
        # Reads entire file into memory
        return f.read()


# Anti-pattern 14: Race condition in file write
def write_concurrent(path: str, data: str):
    # Two processes could write simultaneously
    with open(path, 'w') as f:
        f.write(data)


# Anti-pattern 15: Recursive function without base case guard
def factorial(n):
    # No check for negative numbers or very large numbers
    if n == 0:
        return 1
    return n * factorial(n - 1)


# Anti-pattern 16: Duplicate code blocks
def calculate_tax_a(amount):
    if amount > 10000:
        tax = amount * 0.3
    elif amount > 5000:
        tax = amount * 0.2
    else:
        tax = amount * 0.1
    return tax


def calculate_tax_b(amount):
    if amount > 10000:
        tax = amount * 0.3
    elif amount > 5000:
        tax = amount * 0.2
    else:
        tax = amount * 0.1
    return tax


# Anti-pattern 17: Unclosed resources
def process_data():
    conn = sqlite3.connect('data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM large_table")
    data = cursor.fetchall()
    # Missing: conn.close()
    return data


# Anti-pattern 18: String concatenation in loop
def build_report(items: list) -> str:
    report = ""
    for item in items:
        report += str(item) + "\n"  # O(n²) complexity
    return report


# Anti-pattern 19: Using mutable class variable
class UserRegistry:
    users = []  # Shared across all instances!
    
    def add_user(self, user):
        self.users.append(user)


# Anti-pattern 20: No error handling for file operations
def load_config_file(path: str):
    f = open(path, 'r')
    content = f.read()
    return json.loads(content)


if __name__ == "__main__":
    print("Code audit test fixture loaded.")
    print(f"Config: {get_config().settings}")
    print(f"Factorial of 10: {factorial(10)}")
