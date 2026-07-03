#!/usr/bin/env python3
"""
Claude Trading Skills - Railway Web Server
Serves skill dashboard and health endpoints
"""
import os
import json
import glob
import logging
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SKILLS_DIR = BASE_DIR / "skills"
PORT = int(os.environ.get("PORT", 8080))

def load_skills():
    """Load all skills from skills/ directory"""
    skills = []
    if not SKILLS_DIR.exists():
        logger.warning(f"Skills directory not found at {BASE_DIR}")
        return skills
    
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        try:
            content = skill_md.read_text(encoding="utf-8")
            # Extract description from first non-empty line after name
            lines = [l.strip() for l in content.split("\n") if l.strip()]
            desc = lines[1] if len(lines) > 1 else "No description"
            skills.append({
                "name": skill_name,
                "description": desc[:120],
                "path": str(skill_md.relative_to(BASE_DIR))
            })
        except Exception as e:
            logger.error(f"Error loading {skill_name}: {e}")
    
    return skills

def generate_dashboard():
    """Generate HTML dashboard"""
    skills = load_skills()
    skill_count = len(skills)
    
    skill_cards = ""
    for s in skills:
        skill_cards += f"""
        <div class="skill-card">
            <h3>🔧 {s['name']}</h3>
            <p>{s['description']}</p>
        </div>"""
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Claude Trading Skills</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, sans-serif; background: #0a0a0a; color: #e0e0e0; }}
        header {{ background: #111; border-bottom: 1px solid #333; padding: 20px 40px; }}
        header h1 {{ color: #00ff88; font-size: 1.8rem; }}
        header p {{ color: #888; margin-top: 4px; }}
        .stats {{ display: flex; gap: 20px; padding: 30px 40px; }}
        .stat {{ background: #111; border: 1px solid #222; border-radius: 12px; padding: 20px 30px; }}
        .stat .num {{ font-size: 2.5rem; font-weight: bold; color: #00ff88; }}
        .stat .label {{ color: #888; font-size: 0.9rem; margin-top: 4px; }}
        .skills-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; padding: 0 40px 40px; }}
        .skill-card {{ background: #111; border: 1px solid #222; border-radius: 12px; padding: 20px; transition: border-color 0.2s; }}
        .skill-card:hover {{ border-color: #00ff88; }}
        .skill-card h3 {{ color: #fff; font-size: 1rem; margin-bottom: 8px; }}
        .skill-card p {{ color: #666; font-size: 0.85rem; line-height: 1.5; }}
        .badge {{ display: inline-block; background: #00ff8822; color: #00ff88; border: 1px solid #00ff8844; border-radius: 20px; padding: 4px 12px; font-size: 0.8rem; margin-top: 8px; }}
    </style>
</head>
<body>
    <header>
        <h1>⚡ Claude Trading Skills</h1>
        <p>Automated trading infrastructure — Railway deployment</p>
    </header>
    <div class="stats">
        <div class="stat">
            <div class="num">{skill_count}</div>
            <div class="label">Skills Loaded</div>
        </div>
        <div class="stat">
            <div class="num" style="color:#4af">🟢</div>
            <div class="label">Status: Online</div>
        </div>
        <div class="stat">
            <div class="num" style="font-size:1.2rem; padding-top:8px">{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</div>
            <div class="label">Last Updated</div>
        </div>
    </div>
    <div class="skills-grid">
        {skill_cards}
    </div>
</body>
</html>"""

class TradingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")
    
    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def do_HEAD(self):
        if self.path == "/" or self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
        elif self.path in ["/health", "/api/skills"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            self.send_html(generate_dashboard())

        elif self.path == "/health":
            skills = load_skills()
            self.send_json({
                "status": "healthy",
                "skills_count": len(skills),
                "skills_dir": str(SKILLS_DIR),
                "skills_dir_exists": SKILLS_DIR.exists(),
                "timestamp": datetime.utcnow().isoformat()
            })
        
        elif self.path == "/api/skills":
            self.send_json({
                "skills": load_skills(),
                "total": len(load_skills())
            })
        
        else:
            self.send_json({"error": "Not found"}, 404)

if __name__ == "__main__":
    logger.info(f"Starting Claude Trading Skills server on port {PORT}")
    logger.info(f"Skills directory: {SKILLS_DIR}")
    logger.info(f"Skills found: {len(load_skills())}")
    
    server = HTTPServer(("0.0.0.0", PORT), TradingHandler)
    logger.info(f"Server running at http://0.0.0.0:{PORT}")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped")
        server.server_close()
