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
