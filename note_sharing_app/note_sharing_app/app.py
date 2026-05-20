from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import sqlite3
import os
from datetime import datetime
import zipfile
import io
from pathlib import Path

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# Create uploads folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Database initialization
def init_db():
    """Initialize SQLite database with required tables"""
    conn = sqlite3.connect('notes.db')
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Notes table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            description TEXT,
            filename TEXT,
            file_path TEXT,
            file_size INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')
    
    # Create index for faster queries
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_subject ON notes(user_id, subject)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_title ON notes(user_id, title)')
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()

# Database helper functions
def get_db_connection():
    """Get database connection"""
    conn = sqlite3.connect('notes.db')
    conn.row_factory = sqlite3.Row
    return conn

def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== AUTHENTICATION ROUTES ====================

@app.route('/')
def index():
    """Home page - redirect to dashboard if logged in"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not username or not email or not password:
            return render_template('register.html', error='All fields are required')
        
        if len(username) < 3:
            return render_template('register.html', error='Username must be at least 3 characters')
        
        if len(password) < 6:
            return render_template('register.html', error='Password must be at least 6 characters')
        
        if password != confirm_password:
            return render_template('register.html', error='Passwords do not match')
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            hashed_password = generate_password_hash(password)
            cursor.execute(
                'INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                (username, email, hashed_password)
            )
            conn.commit()
            conn.close()
            
            return redirect(url_for('login', success='Registration successful! Please login.'))
        
        except sqlite3.IntegrityError:
            return render_template('register.html', error='Username or email already exists')
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return render_template('login.html', error='Username and password required')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        conn.close()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error='Invalid username or password')
    
    success = request.args.get('success')
    return render_template('login.html', success=success)

@app.route('/logout')
def logout():
    """User logout"""
    session.clear()
    return redirect(url_for('login'))

# ==================== DASHBOARD & NOTE ROUTES ====================

@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get statistics
    cursor.execute('SELECT COUNT(*) as total FROM notes WHERE user_id = ?', (user_id,))
    total_notes = cursor.fetchone()['total']
    
    cursor.execute('SELECT SUM(file_size) as total_size FROM notes WHERE user_id = ?', (user_id,))
    total_size = cursor.fetchone()['total_size'] or 0
    
    cursor.execute('SELECT COUNT(DISTINCT subject) as total FROM notes WHERE user_id = ?', (user_id,))
    total_subjects = cursor.fetchone()['total']
    
    # Get recent notes
    cursor.execute('''
        SELECT id, title, subject, uploaded_at, file_size 
        FROM notes 
        WHERE user_id = ? 
        ORDER BY uploaded_at DESC 
        LIMIT 5
    ''', (user_id,))
    recent_notes = cursor.fetchall()
    
    conn.close()
    
    stats = {
        'total_notes': total_notes,
        'total_size': f"{total_size / (1024*1024):.2f}" if total_size > 0 else "0",
        'total_subjects': total_subjects
    }
    
    return render_template('dashboard.html', stats=stats, recent_notes=recent_notes)

@app.route('/upload', methods=['POST'])
@login_required
def upload_note():
    """Upload a note file"""
    user_id = session['user_id']
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400
    
    file = request.files['file']
    title = request.form.get('title', '').strip()
    subject = request.form.get('subject', '').strip()
    description = request.form.get('description', '').strip()
    
    if not file or file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    if not title or not subject:
        return jsonify({'success': False, 'error': 'Title and subject are required'}), 400
    
    try:
        filename = secure_filename(file.filename)
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400
            
        # Add timestamp to filename to avoid conflicts
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
        stored_filename = timestamp + filename
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], stored_filename)
        
        # Ensure uploads folder exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        
        file.save(file_path)
        file_size = os.path.getsize(file_path)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notes (user_id, title, subject, description, filename, file_path, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, title, subject, description, filename, stored_filename, file_size))
        
        note_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'File uploaded successfully',
            'file_size': f"{file_size / 1024:.2f}" if file_size > 0 else "0",
            'note_id': note_id,
            'title': title,
            'subject': subject
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/notes')
@login_required
def notes():
    """View all notes"""
    user_id = session['user_id']
    search = request.args.get('search', '').strip()
    subject_filter = request.args.get('subject', '').strip()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all unique subjects for filter
    cursor.execute('SELECT DISTINCT subject FROM notes WHERE user_id = ? ORDER BY subject', (user_id,))
    subjects = [row['subject'] for row in cursor.fetchall()]
    
    # Build query based on filters
    query = 'SELECT * FROM notes WHERE user_id = ?'
    params = [user_id]
    
    if search:
        query += ' AND (title LIKE ? OR description LIKE ? OR subject LIKE ?)'
        search_param = f'%{search}%'
        params.extend([search_param, search_param, search_param])
    
    if subject_filter:
        query += ' AND subject = ?'
        params.append(subject_filter)
    
    query += ' ORDER BY uploaded_at DESC'
    
    cursor.execute(query, params)
    notes_list = cursor.fetchall()
    conn.close()
    
    return render_template('notes.html', notes=notes_list, subjects=subjects, 
                         search=search, subject_filter=subject_filter)

@app.route('/note/<int:note_id>')
@login_required
def view_note(note_id):
    """View note details"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    note = cursor.fetchone()
    conn.close()
    
    if not note:
        return redirect(url_for('notes'))
    
    return render_template('note_detail.html', note=note)

@app.route('/download/<int:note_id>')
@login_required
def download_note(note_id):
    """Download a single note"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    note = cursor.fetchone()
    conn.close()
    
    if not note:
        return redirect(url_for('notes'))
    
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], note['file_path'])
    
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=note['filename'])
    
    return redirect(url_for('notes'))

@app.route('/download-subject/<subject>')
@login_required
def download_subject(subject):
    """Download all notes for a subject as ZIP"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM notes WHERE user_id = ? AND subject = ?', (user_id, subject))
    notes_list = cursor.fetchall()
    conn.close()
    
    if not notes_list:
        return redirect(url_for('notes'))
    
    # Create ZIP file
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for note in notes_list:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], note['file_path'])
            if os.path.exists(file_path):
                zip_file.write(file_path, arcname=note['filename'])
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{subject}_notes.zip'
    )

@app.route('/delete/<int:note_id>', methods=['POST'])
@login_required
def delete_note(note_id):
    """Delete a note"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify ownership
    cursor.execute('SELECT file_path FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    note = cursor.fetchone()
    
    if note:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], note['file_path'])
        # Delete file from disk
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        
        # Delete from database
        cursor.execute('DELETE FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
        conn.commit()
    
    conn.close()
    
    return jsonify({'success': True, 'message': 'Note deleted successfully'})

# ==================== API ROUTES ====================

@app.route('/api/subjects')
@login_required
def api_subjects():
    """Get all subjects with note count"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT subject, COUNT(*) as count 
        FROM notes 
        WHERE user_id = ? 
        GROUP BY subject 
        ORDER BY subject
    ''', (user_id,))
    
    subjects = [{'subject': row['subject'], 'count': row['count']} for row in cursor.fetchall()]
    conn.close()
    
    return jsonify(subjects)

@app.route('/api/stats')
@login_required
def api_stats():
    """Get user statistics"""
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) as total FROM notes WHERE user_id = ?', (user_id,))
    total_notes = cursor.fetchone()['total']
    
    cursor.execute('SELECT SUM(file_size) as total FROM notes WHERE user_id = ?', (user_id,))
    total_size = cursor.fetchone()['total'] or 0
    
    cursor.execute('SELECT COUNT(DISTINCT subject) as total FROM notes WHERE user_id = ?', (user_id,))
    total_subjects = cursor.fetchone()['total']
    
    conn.close()
    
    return jsonify({
        'total_notes': total_notes,
        'total_size_mb': round(total_size / (1024*1024), 2),
        'total_subjects': total_subjects
    })

@app.route('/api/search')
@login_required
def api_search():
    """Search notes"""
    user_id = session['user_id']
    query = request.args.get('q', '').strip()
    
    if not query:
        return jsonify([])
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    search_param = f'%{query}%'
    cursor.execute('''
        SELECT id, title, subject, uploaded_at 
        FROM notes 
        WHERE user_id = ? AND (title LIKE ? OR description LIKE ? OR subject LIKE ?)
        ORDER BY uploaded_at DESC
        LIMIT 10
    ''', (user_id, search_param, search_param, search_param))
    
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify(results)

# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(error):
    """404 error handler"""
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    """500 error handler"""
    return render_template('500.html'), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
