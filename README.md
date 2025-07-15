

````markdown
# Real-Time AI-Based Energy Monitoring System (Backend + AI Model)

This repository contains the backend server and AI model for a real-time appliance monitoring system, built as part of my graduation project. The system uses a single current sensor and a ConvLSTM deep learning model to detect when appliances are plugged in or out, calculate energy usage, and communicate with a mobile app in real-time.

---

## 📌 Project Summary

- 🧠 **AI Model**: ConvLSTM trained on current readings with 92.9% accuracy  
- ⚙️ **Backend**: Built with FastAPI  
- 🌐 **WebSocket**: Real-time communication with the mobile app  
- 🔌 **Hardware**: ESP32 sends current data (handled separately)  
- 🗃️ **Database**: MongoDB Atlas (cloud-based)

> 🚨 *Note: The mobile application and ESP32 firmware are not included in this repo.*

---

## 🛠️ Tech Stack

- Python 3.9+
- FastAPI
- TensorFlow / Keras (ConvLSTM)
- NumPy, Pandas, Scikit-learn
- MongoDB (via `motor`)
- WebSockets (`fastapi.websockets`)
- Uvicorn (ASGI server)

---

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/Merna-Hany12/energy-monitoring-backend.git
cd energy-monitoring-backend
````

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure the MongoDB connection

In `main.py` or `db.py`, replace the placeholder with your actual connection string:

```python
DATABASE_URL = "mongodb+srv://<username>:<password>@cluster.mongodb.net/<dbname>"
```

> 🔐 **Important**: Never push your real credentials. You can also use a `config.py` or `.env` file for better security.

### 4. Run the server

```bash
uvicorn main:app --reload
```

Open your browser to:
👉 `http://localhost:8000/docs` for Swagger API documentation

---

## 🧠 AI Model Details

* Type: Convolutional Long Short-Term Memory (ConvLSTM)
* Accuracy: **92.9%**
* Input: Sequence of 20 current readings
* Output: Predicted appliance state (plugged in / unplugged)
* Model Files:

  * `models/convlstm_model.h5`
  * `models/scaler.pkl`
  * `models/label_encoder.pkl`

---

## 📝 Notes

* This project is part of an academic graduation project and is continuously improving.
* The AI model was trained and tested offline; integration for live training is possible in future work.

---


## 🙋‍♀️ Author

**Merna Hany**
🔗 [GitHub](https://github.com/Merna-Hany12)
