#!/usr/bin/env python3
"""
Minimal HTTP server for SoniXcape preset management
Works alongside your existing ws_audio.py WebSocket server
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime
from aiohttp import web
import aiohttp_cors

# Configuration
HTTP_PORT = 8090
BASE_DIR = Path("/opt/sonixscape")
WEBUI_DIR = BASE_DIR / "webui"
DATA_DIR = WEBUI_DIR / "data"
PRESETS_DIR = DATA_DIR / "presets"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRESETS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_ROW = {
    'time': 60, 'strength': 5, 'frequency': 100, 'freqSweep': 5, 'sweepSpeed': 1,
    'neck': 5, 'back': 5, 'thighs': 5, 'legs': 5, 'modFreq': 1, 'phase': 0, 
    'tapDuty': 0, 'mode': 0
}

class PresetManager:
    def __init__(self, presets_dir: Path):
        self.presets_dir = presets_dir
        self._ensure_default_preset()
    
    def _ensure_default_preset(self):
        """Create a default preset if none exist"""
        if not list(self.presets_dir.glob("*.json")):
            default_preset = {
                "id": "preset-default",
                "name": "Default Treatment",
                "description": "Default treatment parameters",
                "category": "basic",
                "created": datetime.now().isoformat(),
                "modified": datetime.now().isoformat(),
                "rows": [dict(DEFAULT_ROW) for _ in range(6)]
            }
            self.save_preset(default_preset)
            print("[PRESET] Created default preset")
    
    def list_presets(self):
        """List all available presets"""
        presets = []
        for preset_file in self.presets_dir.glob("*.json"):
            try:
                with open(preset_file, 'r') as f:
                    preset = json.load(f)
                    if all(k in preset for k in ['id', 'name', 'rows']):
                        presets.append(preset)
            except Exception as e:
                print(f"[PRESET] Error loading {preset_file}: {e}")
        
        presets.sort(key=lambda p: p.get('name', '').lower())
        return presets
    
    def save_preset(self, preset):
        """Save a preset to disk"""
        try:
            if not all(k in preset for k in ['id', 'name', 'rows']):
                raise ValueError("Missing required fields: id, name, rows")
            
            preset['modified'] = datetime.now().isoformat()
            if 'created' not in preset:
                preset['created'] = preset['modified']
            
            preset_file = self.presets_dir / f"{preset['id']}.json"
            with open(preset_file, 'w') as f:
                json.dump(preset, f, indent=2)
            
            print(f"[PRESET] Saved: {preset['name']}")
            return True
        except Exception as e:
            print(f"[PRESET] Save error: {e}")
            return False
    
    def delete_preset(self, preset_id):
        """Delete a preset"""
        try:
            preset_file = self.presets_dir / f"{preset_id}.json"
            if preset_file.exists():
                preset_file.unlink()
                print(f"[PRESET] Deleted: {preset_id}")
                return True
            return False
        except Exception as e:
            print(f"[PRESET] Delete error: {e}")
            return False

# HTTP Route Handlers
async def list_presets_handler(request):
    preset_manager = request.app['preset_manager']
    presets = preset_manager.list_presets()
    return web.json_response(presets)

async def save_preset_handler(request):
    try:
        preset = await request.json()
        preset_manager = request.app['preset_manager']
        
        if 'id' not in preset:
            import time, uuid
            preset['id'] = f"preset-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        
        success = preset_manager.save_preset(preset)
        if success:
            return web.json_response({'success': True, 'id': preset['id']})
        else:
            return web.json_response({'error': 'Failed to save preset'}, status=500)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=400)

async def delete_preset_handler(request):
    try:
        data = await request.json()
        preset_id = data.get('id')
        if not preset_id:
            return web.json_response({'error': 'Missing preset ID'}, status=400)
        
        preset_manager = request.app['preset_manager']
        success = preset_manager.delete_preset(preset_id)
        
        return web.json_response({'success': success})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=400)

async def serve_index(request):
    """Serve the main HTML file"""
    html_path = WEBUI_DIR / "index.html"
    if html_path.exists():
        return web.FileResponse(html_path)
    else:
        return web.Response(text="index.html not found. Please check webui/index.html exists.", status=404)

async def serve_manifest(request):
    """Serve manifest.json"""
    manifest_path = BASE_DIR / "manifest.json"
    if manifest_path.exists():
        return web.FileResponse(manifest_path)
    else:
        return web.json_response({"error": "manifest.json not found"}, status=404)

def create_app():
    app = web.Application()
    
    # Initialize preset manager
    preset_manager = PresetManager(PRESETS_DIR)
    app['preset_manager'] = preset_manager
    
    # Setup CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    # Routes
    app.router.add_get('/list-presets', list_presets_handler)
    app.router.add_post('/save-preset', save_preset_handler)
    app.router.add_post('/delete-preset', delete_preset_handler)
    app.router.add_get('/', serve_index)
    app.router.add_get('/index.html', serve_index)
    app.router.add_get('/manifest.json', serve_manifest)
    
    # Add CORS to all routes
    for route in list(app.router.routes()):
        cors.add(route)
    
    return app

async def main():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    
    print(f"[HTTP] Preset server running on port {HTTP_PORT}")
    print(f"[INFO] Web interface: http://localhost:{HTTP_PORT}")
    print(f"[INFO] Make sure ws_audio.py is running on port 8081 for audio")
    print(f"[INFO] Serving files from: {WEBUI_DIR}")
    
    try:
        await asyncio.Future()  # run forever
    except KeyboardInterrupt:
        print("[HTTP] Shutting down...")
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
