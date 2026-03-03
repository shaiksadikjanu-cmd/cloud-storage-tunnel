import eventlet
# This MUST be the very first thing in the file for WebSockets to work!
eventlet.monkey_patch()

import os
import uuid
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, emit
from eventlet.event import Event

app = Flask(__name__)
app.config['SECRET_KEY'] = 'janu_startup_master_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///januos_cloud.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize the blazing-fast WebSocket server!
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# These dictionaries hold the live connections to your physical laptops
active_hardware_nodes = {} # Maps physical node_id to their live WebSocket session ID
pending_upload_requests = {}
pending_file_requests = {} # Holds the user's browser open until the laptop replies

# ==========================================
# 🗄️ DATABASE SCHEMA
# ==========================================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(150), nullable=False)
    nodes = db.relationship('CloudNode', backref='owner', lazy=True)

class CloudNode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.String(50), unique=True, nullable=False) 
    api_key = db.Column(db.String(100), unique=True, nullable=False) 
    name = db.Column(db.String(100), nullable=False) 
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==========================================
# 🔌 THE HARDWARE WEBSOCKET BRIDGE
# ==========================================

@socketio.on('hardware_connect')
def handle_hardware_connect(data):
    """When cloud_server.py boots up, it knocks on this door."""
    node_id = data.get('node_id')
    
    # Verify this node is registered in the database
    node = CloudNode.query.filter_by(node_id=node_id).first()
    if node:
        active_hardware_nodes[node_id] = request.sid
        print(f"✅ PHYSICAL DRIVE SECURED: {node.name} ({node_id}) is ONLINE!")
        emit('bridge_success', {'status': 'Connected to JanuOS Relay Server'})
    else:
        print(f"❌ UNREGISTERED DRIVE BLOCKED: {node_id}")
        emit('bridge_error', {'error': 'Node not registered in Dashboard.'})

@socketio.on('hardware_files_response')
def handle_files_response(data):
    """When your physical laptop replies with the files, we pass them to the UI."""
    req_id = data.get('req_id')
    if req_id in pending_file_requests:
        pending_file_requests[req_id].send(data)

@socketio.on('disconnect')
def handle_disconnect():
    """If your laptop goes to sleep, we remove it from the active list."""
    for node_id, sid in list(active_hardware_nodes.items()):
        if sid == request.sid:
            del active_hardware_nodes[node_id]
            print(f"⚠️ PHYSICAL DRIVE OFFLINE: {node_id}")

# ==========================================
# 📡 THE API GATEWAY (Where app.py connects)
# ==========================================

@socketio.on('hardware_upload_response')
def handle_upload_response(data):
    req_id = data.get('req_id')
    if req_id in pending_upload_requests:
        pending_upload_requests[req_id].send(data)

@app.route('/api/files', methods=['GET'])
def proxy_files():
    api_key = request.headers.get('X-API-Key')
    if not api_key: return jsonify({'error': 'No API Key'}), 401
    node = CloudNode.query.filter_by(api_key=api_key).first()
    if not node: return jsonify({'error': 'Invalid API Key'}), 403
    hardware_sid = active_hardware_nodes.get(node.node_id)
    if not hardware_sid: return jsonify({'error': 'Physical Drive is Offline!'}), 503
    
    req_id = str(uuid.uuid4())
    pending_file_requests[req_id] = Event()
    
    # We now pass the specific folder the user clicked on!
    folder_id = request.args.get('folder_id', '')
    socketio.emit('cmd_get_files', {'req_id': req_id, 'folder_id': folder_id}, room=hardware_sid)
    
    try:
        result = pending_file_requests[req_id].wait(timeout=10)
        return jsonify({'files': result.get('files', [])})
    except eventlet.timeout.Timeout:
        return jsonify({'error': 'Hardware took too long to respond'}), 504
    finally:
        if req_id in pending_file_requests: del pending_file_requests[req_id]

@app.route('/api/upload', methods=['POST'])
def proxy_upload():
    """The missing Upload Route!"""
    api_key = request.headers.get('X-API-Key')
    node = CloudNode.query.filter_by(api_key=api_key).first()
    hardware_sid = active_hardware_nodes.get(node.node_id) if node else None
    
    if not hardware_sid: return jsonify({'error': 'Node Offline'}), 503
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    
    file = request.files['file']
    folder_id = request.form.get('folder_id', '')
    req_id = str(uuid.uuid4())
    pending_upload_requests[req_id] = Event()

    # Pass the actual file bytes through the WebSocket!
    socketio.emit('cmd_upload_file', {
        'req_id': req_id,
        'folder_id': folder_id,
        'filename': file.filename,
        'file_data': file.read() 
    }, room=hardware_sid)

    try:
        result = pending_upload_requests[req_id].wait(timeout=15)
        return jsonify(result)
    except eventlet.timeout.Timeout:
        return jsonify({'error': 'Upload timed out'}), 504
    finally:
        if req_id in pending_upload_requests: del pending_upload_requests[req_id]

# ... (KEEP YOUR DASHBOARD AND AUTH ROUTES EXACTLY THE SAME BELOW THIS LINE) ...
@app.route('/')
@login_required
def dashboard():
    user_nodes = CloudNode.query.filter_by(user_id=current_user.id).all()
    return render_template_string(DASHBOARD_HTML, user=current_user, nodes=user_nodes)

@app.route('/add-node', methods=['POST'])
@login_required
def add_node():
    node_id = request.form.get('node_id').strip()
    node_name = request.form.get('node_name').strip()
    if CloudNode.query.filter_by(node_id=node_id).first():
        flash('This Device ID is already registered!', 'error')
        return redirect(url_for('dashboard'))
    new_node = CloudNode(node_id=node_id, name=node_name, api_key=f"janu_api_{uuid.uuid4().hex}", user_id=current_user.id)
    db.session.add(new_node)
    db.session.commit()
    flash('Storage Node successfully linked to your JanuOS Account!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return redirect(url_for('register'))
        db.session.add(User(username=username, password_hash=generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')))
        db.session.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template_string(AUTH_HTML, action="Register")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template_string(AUTH_HTML, action="Login")

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

AUTH_HTML = """<!DOCTYPE html><html><head><title>JanuOS - {{ action }}</title><style>body{font-family:sans-serif; background:#121212; color:white; display:flex; justify-content:center; align-items:center; height:100vh;} .box{background:#1e1e1e; padding:30px; border-radius:10px; text-align:center;} input, button{width:100%; margin:10px 0; padding:10px;} button{background:#3b82f6; color:white; border:none; border-radius:5px; cursor:pointer;}</style></head><body><div class="box"><h2>JanuOS Cloud {{ action }}</h2>{% with messages = get_flashed_messages() %}{% if messages %}<p style="color:red;">{{ messages[0] }}</p>{% endif %}{% endwith %}<form method="POST"><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">{{ action }}</button></form><a href="{{ url_for('login' if action == 'Register' else 'register') }}" style="color:#aaa;">Go to {{ 'Login' if action == 'Register' else 'Register' }}</a></div></body></html>"""
DASHBOARD_HTML = """<!DOCTYPE html><html><head><title>JanuOS Dashboard</title><style>body{font-family:sans-serif; background:#121212; color:white; padding:40px;} .card{background:#1e1e1e; padding:20px; border-radius:10px; margin-bottom:20px;} input, button{padding:10px; margin:5px 0;} button{background:#27c93f; color:white; border:none; border-radius:5px; cursor:pointer;} .api-key{background:#000; padding:10px; font-family:monospace; color:#0f0; border-radius:5px;}</style></head><body><div style="display:flex; justify-content:space-between; align-items:center;"><h2>Welcome, {{ user.username }}!</h2><a href="{{ url_for('logout') }}" style="color:#ff5f56;">Logout</a></div><div class="card"><h3>➕ Link New Storage Node</h3><form method="POST" action="{{ url_for('add_node') }}"><input type="text" name="node_name" placeholder="Name (e.g. My 16GB Pendrive)" required><input type="text" name="node_id" placeholder="Device ID (e.g. JANU_NODE_123)" required><button type="submit">Connect Node</button></form></div><h3>Your Connected Storage</h3>{% for node in nodes %}<div class="card"><h4>{{ node.name }}</h4><p><strong>Device ID:</strong> {{ node.node_id }}</p><p><strong>API Key:</strong></p><div class="api-key">{{ node.api_key }}</div></div>{% else %}<p style="color:#aaa;">No storage nodes linked yet.</p>{% endfor %}</body></html>"""

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    print("🌐 JANUOS CLOUD RELAY STARTED ON PORT 8080 🌐")
    # MUST run via socketio.run, not app.run!
    socketio.run(app, host='0.0.0.0', port=8080, debug=True)
