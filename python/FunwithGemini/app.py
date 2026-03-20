import os, sqlite3, secrets, re
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "letgo-style-pro-v9"

# --- CONFIGURATION ---
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- DATABASE & AUTO-MIGRATION ---
def get_db_connection():
    conn = sqlite3.connect('marketplace.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')
    
    # 1. Ensure the table exists
    conn.execute('''CREATE TABLE IF NOT EXISTS vehicles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        make TEXT, 
        price INTEGER)''')

    # 2. FORCE REPAIR: Check for image_path specifically
    cursor = conn.execute("PRAGMA table_info(vehicles)")
    columns = [row['name'] for row in cursor.fetchall()]
    
    # List of all columns required for the LetGo layout to work
    required = {
        "image_path": "TEXT",
        "model": "TEXT",
        "location": "TEXT",
        "year": "INTEGER",
        "mileage": "INTEGER",
        "user_id": "INTEGER",
        "lat": "REAL",
        "lng": "REAL"
    }

    for col, col_type in required.items():
        if col not in columns:
            print(f"MIGRATION: Adding missing column '{col}'...")
            conn.execute(f"ALTER TABLE vehicles ADD COLUMN {col} {col_type}")

    conn.commit()
    conn.close()
    print("✅ Database repair successful.")



# --- DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ROUTES ---
@app.route('/')
def home():
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    # Explicitly fetching cars to ensure they exist for the template
    cars = conn.execute("SELECT * FROM vehicles ORDER BY id DESC").fetchall()
    conn.close()
    
    # Passing cars=cars is what makes the {% for %} loop work
    return render_template('index.html', cars=cars)


@app.route('/create', methods=['POST'])
@login_required
def create_listing():
    def to_int(value, default=0):
        try:
            if value is None or str(value).strip() == '':
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    files = request.files.getlist('photos')
    filenames = []
    for file in files:
        if file and file.filename != '':
            fname = secure_filename(file.filename)
            unique_fname = secrets.token_hex(4) + "_" + fname
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_fname))
            filenames.append(unique_fname)
    
    if filenames:
        image_string = ",".join(filenames)
        conn = get_db_connection()
        conn.execute('''INSERT INTO vehicles (user_id, make, model, year, price, mileage, location, lat, lng, image_path) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (
                                session['user_id'],
                                request.form.get('make', '').strip(),
                                request.form.get('model', '').strip(),
                                to_int(request.form.get('year')),
                                to_int(request.form.get('price')),
                                to_int(request.form.get('mileage')),
                                request.form.get('location', '').strip(),
                                request.form.get('lat'),
                                request.form.get('lng'),
                                image_string,
                            ))
        conn.commit()
        conn.close()
    return redirect(url_for('home'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user, pwd = request.form['username'], request.form['password']
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (user,)).fetchone()
        conn.close()
        if row and check_password_hash(row['password'], pwd):
            session['user_id'], session['username'] = row['id'], row['username']
            return redirect(url_for('home'))
        return render_template('login.html', error="Invalid credentials.")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user, pwd = request.form['username'], generate_password_hash(request.form['password'])
        conn = get_db_connection()
        try:
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user, pwd))
            conn.commit()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            return render_template('register.html', error="Username taken.")
        finally: conn.close()
    return render_template('register.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, use_reloader=False)
