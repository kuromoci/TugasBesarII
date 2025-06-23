from flask import Flask, render_template, url_for, jsonify, redirect, request, session
import requests
import json
import os
import datetime
from pathlib import Path
from supabase import create_client, Client

# --- KONFIGURASI FLASK DAN LOKASI FILE ---
BASE_DIR = Path(__file__).resolve().parent

app = Flask(
  __name__, 
  template_folder=os.path.join(BASE_DIR.parent, 'html'), 
  static_folder=os.path.join(BASE_DIR.parent, 'css'),
  static_url_path='/css'
)

app.secret_key = os.environ.get('FLASK_SECRET_KEY', '84f742565e9f38596df9e50653af9321e70877804582b778')

# Supabase URL dan Key (dari Environment Variables di Vercel Dashboard)
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

# Inisialisasi Supabase client (di luar route untuk reusability)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

VALID_USERNAME = os.environ.get('APP_USERNAME', 'sosokAdmin')
VALID_PASSWORD = os.environ.get('APP_PASSWORD', 'admin1234')
# Tidak lagi perlu ESP32_IP di sini, karena ESP32 akan komunikasi ke API Vercel
# ESP32_IP = 'IP_ADDRESS_ANDA'

# last_esp32_data tidak lagi menjadi sumber utama data, tapi bisa dipakai untuk default
last_esp32_data = {
  'load_voltage': 0.0,      
  'current_mA': 0.0,        
  'power_W': 0.0,           
  'digipot_step': 50,       # Tambahkan ini untuk langkah digipot
  'digipot_analog_read': 0.0, # Tambahkan ini untuk pembacaan analog digipot
  'target_digipot_step': 50, # Target langkah digipot (nilai awal slider)
  'relay_status': 'OFF'     
}


# ==============================================================================
#                               ROUTES WEB
# ==============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
  error = None 
  if request.method == 'POST':
    username = request.form.get('username')
    password = request.form.get('password')

    if username == VALID_USERNAME and password == VALID_PASSWORD:
      session['logged_in'] = True
      return redirect(url_for('dashboard'))
    else:
      return render_template('login.html', error='Invalid Credentials, please try again.'), 401
  return render_template('login.html', error=error)

@app.route('/')
def dashboard():
  if not session.get('logged_in'):
    return redirect(url_for('login'))
  
  # Untuk dashboard initial load, kita akan mengambil data dari Supabase
  # Data ini akan segera di-update oleh JavaScript getLiveData()
  initial_data = get_latest_sensor_data_and_settings_from_supabase()
  if initial_data:
      data_to_render = initial_data
  else:
      # Jika tidak ada data di Supabase, gunakan default Flask
      data_to_render = last_esp32_data 

  return render_template('dashboard.html', data=data_to_render, esp32_ip="N/A") 
  # esp32_ip tidak lagi relevan untuk komunikasi langsung, bisa di-set "N/A"

@app.route('/logout')
def logout():
  session.pop('logged_in', None)
  return redirect(url_for('login'))
  
# ==============================================================================
#                               API ROUTES (FRONTEND <-> FLASK <-> SUPABASE)
# ==============================================================================

# API untuk mendapatkan data live dari Supabase (dipanggil oleh JavaScript frontend)
@app.route('/get_live_data', methods=['GET'])
def get_live_data():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        latest_data = get_latest_sensor_data_and_settings_from_supabase()
        if latest_data:
            return jsonify(latest_data), 200
        else:
            # Jika tidak ada data sama sekali, kirim default state
            return jsonify(last_esp32_data), 200 
    except Exception as e:
        print(f"Error fetching live data from Supabase: {e}")
        return jsonify({"error": f"Failed to fetch data: {e}"}), 500

# API untuk mengatur output Digipot (akan menulis ke Supabase 'device_settings')
@app.route('/update_output_params', methods=['POST'])
def update_output_params():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401

    if request.is_json:
        data = request.get_json()
        target_digipot_step = data.get('digipot_step') # Namanya diubah menjadi 'digipot_step'

        if target_digipot_step is None: 
            return jsonify({"error": "Missing digipot_step parameter"}), 400
        
        try:
            target_digipot_step = int(target_digipot_step) # Langkah digipot biasanya int
        except (ValueError, TypeError):
             return jsonify({"error": "Invalid digipot_step format"}), 400

        try:
            success = update_digipot_step_in_supabase(target_digipot_step)
            if success:
                return jsonify({"message": f"Target digipot step updated to {target_digipot_step}. ESP32 will pick it up."}), 200
            else:
                return jsonify({"error": "Failed to update digipot step in database."}), 500
        except Exception as e:
            print(f"Error updating digipot step in Supabase: {e}")
            return jsonify({"error": f"Failed to update digipot step: {e}"}), 500
    return jsonify({"error": "Request must be JSON"}), 400

# API untuk mengontrol relay (akan menulis ke Supabase 'device_settings')
@app.route('/control_relay', methods=['POST'])
def control_relay():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401

    if request.is_json:
        data = request.get_json()
        status = data.get('status')

        if status not in ['on', 'off']:
            return jsonify({"error": "Invalid relay status. Must be 'on' or 'off'."}), 400

        try:
            success = update_relay_status_in_supabase(status)
            if success:
                return jsonify({"message": f"Relay status updated in database to {status.upper()}. ESP32 will pick it up."}), 200
            else:
                return jsonify({"error": "Failed to update relay status in database."}), 500
        except Exception as e:
            print(f"Error updating relay status in Supabase: {e}")
            return jsonify({"error": f"Failed to update relay status: {e}"}), 500
    return jsonify({"error": "Request must be JSON"}), 400

# ==============================================================================
#                  API FLASK UNTUK KOMUNIKASI ESP32 <-> FLASK <-> SUPABASE
# ==============================================================================

# API: ESP32 MENGIRIM DATA SENSOR KE FLASK (untuk disimpan ke Supabase)
@app.route('/upload_sensor_data', methods=['POST'])
def upload_sensor_data():
    # Ini bisa diproteksi dengan API key atau tidak
    # if request.headers.get('X-API-KEY') != "YOUR_ESP32_API_KEY":
    #     return jsonify({"error": "Unauthorized"}), 401

    if request.is_json:
        sensor_data = request.get_json()
        try:
            # Masukkan data ke tabel sensor_readings
            response = supabase.table('sensor_readings').insert({
                'load_voltage': sensor_data.get('load_voltage'),
                'current_mA': sensor_data.get('current_mA'),
                'power_W': sensor_data.get('power_W'),
                'digipot_step': sensor_data.get('digipot_step'),
                'digipot_analog_read': sensor_data.get('digipot_analog_read')
            }).execute()
            print(f"Sensor data uploaded to Supabase: {sensor_data}")
            return jsonify({"message": "Data uploaded successfully"}), 200
        except Exception as e:
            print(f"Error uploading sensor data to Supabase: {e}")
            return jsonify({"error": f"Failed to upload data: {e}"}), 500
    return jsonify({"error": "Invalid JSON"}), 400

# API: ESP32 MENDAPATKAN PERINTAH/SETTING DARI FLASK (membaca dari Supabase)
@app.route('/get_device_commands', methods=['GET'])
def get_device_commands():
    try:
        # Ambil setting terbaru dari tabel device_settings
        response = supabase.table('device_settings').select('*').order('created_at', desc=True).limit(1).execute()
        
        if response.data:
            latest_settings = response.data[0]
            commands = {
                'target_digipot_step': latest_settings.get('target_digipot_step', 50), # Default 50 langkah
                'relay_status': latest_settings.get('relay_status', 'OFF')
            }
            return jsonify(commands), 200
        else:
            # Jika tidak ada setting di DB, kirim default
            return jsonify({'target_digipot_step': 50, 'relay_status': 'OFF'}), 200
    except Exception as e:
        print(f"Error fetching device commands from Supabase: {e}")
        return jsonify({"error": f"Failed to fetch commands: {e}"}), 500

# ==============================================================================
#                  Fungsi Interaksi dengan Supabase (Implementasi)
# ==============================================================================

# Mengambil data sensor terbaru dan setting dari Supabase (digunakan oleh /get_live_data)
def get_latest_sensor_data_and_settings_from_supabase():
    combined_data = {}
    try:
        # Dapatkan data sensor terbaru
        sensor_res = supabase.table('sensor_readings').select('*').order('created_at', desc=True).limit(1).execute()
        if sensor_res.data:
            latest_sensor = sensor_res.data[0]
            combined_data['load_voltage'] = latest_sensor.get('load_voltage', 0.0)
            combined_data['current_mA'] = latest_sensor.get('current_mA', 0.0)
            combined_data['power_W'] = latest_sensor.get('power_W', 0.0)
            combined_data['digipot_step'] = latest_sensor.get('digipot_step', 50)
            combined_data['digipot_analog_read'] = latest_sensor.get('digipot_analog_read', 0.0)
        else: # Default jika tidak ada data sensor
            combined_data.update({'load_voltage': 0.0, 'current_mA': 0.0, 'power_W': 0.0, 'digipot_step': 50, 'digipot_analog_read': 0.0})

        # Dapatkan setting/perintah terbaru
        settings_res = supabase.table('device_settings').select('*').order('created_at', desc=True).limit(1).execute()
        if settings_res.data:
            latest_settings = settings_res.data[0]
            combined_data['target_digipot_step'] = latest_settings.get('target_digipot_step', 50)
            combined_data['relay_status'] = latest_settings.get('relay_status', 'OFF')
        else: # Default jika tidak ada setting
            combined_data.update({'target_digipot_step': 50, 'relay_status': 'OFF'})

        return combined_data

    except Exception as e:
        print(f"Error in get_latest_sensor_data_and_settings_from_supabase: {e}")
        return None # Kembalikan None jika ada error

# Mengupdate langkah digipot di tabel Supabase
def update_digipot_step_in_supabase(step):
    try:
        # Update baris 'device_settings' atau insert jika tidak ada
        # Asumsi ada satu baris setting per device_id 'esp32_monitor_01'
        response = supabase.table('device_settings').update({'target_digipot_step': step, 'created_at': datetime.datetime.now().isoformat()}).eq('device_id', 'esp32_monitor_01').execute()
        if not response.data: 
             # Jika tidak ada yang diupdate (misal device_id belum ada), insert baru
             supabase.table('device_settings').insert({'device_id': 'esp32_monitor_01', 'target_digipot_step': step}).execute()
        return True
    except Exception as e:
        print(f"Error updating digipot step in Supabase: {e}")
        return False

# Mengupdate status relay di tabel Supabase
def update_relay_status_in_supabase(status):
    try:
        response = supabase.table('device_settings').update({'relay_status': status.upper(), 'created_at': datetime.datetime.now().isoformat()}).eq('device_id', 'esp32_monitor_01').execute()
        if not response.data: 
             supabase.table('device_settings').insert({'device_id': 'esp32_monitor_01', 'relay_status': status.upper()}).execute()
        return True
    except Exception as e:
        print(f"Error updating relay status in Supabase: {e}")
        return False

if __name__ == "__main__":
  app.run(debug=True, port=5000)