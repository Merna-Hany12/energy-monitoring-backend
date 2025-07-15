import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from typing import List, Dict, Set 
import numpy as np
from datetime import datetime, timedelta
import pickle
from keras.models import load_model
import json
import pandas as pd
from itertools import combinations
import logging
import asyncio
from pymongo import MongoClient
from bson import ObjectId
from fastapi import Query
from motor.motor_asyncio import AsyncIOMotorClient

from starlette.websockets import WebSocketState  # Add this import
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


MONGODB_URI = "mongodb+srv://<username>:<password>@cluster.mongodb.net/<dbname>"
client = AsyncIOMotorClient(MONGODB_URI)  # CHANGED TO ASYNC CLIENT
db = client["energy"]


# Collections
hardware_col = db.hardware
users_col = db.users
appliance_col = db.appliances

app = FastAPI()

# Hardware Connection Manager
class HardwareConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, hardware_id: str):
        self.active_connections[hardware_id] = websocket
        logger.info(f"Hardware connected: {hardware_id}")

    def disconnect(self, hardware_id: str):
        if hardware_id in self.active_connections:
            del self.active_connections[hardware_id]
            logger.info(f"Hardware disconnected: {hardware_id}")

    async def send_to_hardware(self, message: dict, hardware_id: str):
        if hardware_id in self.active_connections:
            await self.active_connections[hardware_id].send_json(message)

# Mobile Connection Manager
class MobileConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, hardware_id: str):
        if hardware_id not in self.active_connections:
            self.active_connections[hardware_id] = set()
        self.active_connections[hardware_id].add(websocket)
        logger.info(f"Mobile connected to hardware {hardware_id}")

    def disconnect(self, websocket: WebSocket, hardware_id: str):
        if hardware_id in self.active_connections and websocket in self.active_connections[hardware_id]:
            self.active_connections[hardware_id].discard(websocket)
            if not self.active_connections[hardware_id]:
                del self.active_connections[hardware_id]
            logger.info(f"Mobile disconnected from hardware {hardware_id}")

    # THIS METHOD NEEDS PROPER INDENTATION TO BE PART OF THE CLASS
    async def broadcast(self, hardware_id: str, message: dict):
        if hardware_id in self.active_connections:
            for websocket in list(self.active_connections[hardware_id]):
                try:
                    # Convert Timestamps to ISO strings before sending
                    converted_message = convert_timestamps(message)
                    await websocket.send_json(converted_message)
                except Exception as e:
                    logger.error(f"Error sending to mobile: {str(e)}")
                    self.disconnect(websocket, hardware_id)
# Initialize managers
hardware_manager = HardwareConnectionManager()
mobile_manager = MobileConnectionManager()
# Simplified Mobile WebSocket endpoint
@app.websocket("/mobile/ws")
async def mobile_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    hardware_id = None
    counts_received = False  # Track if mobile sent counts
    try:
        # Get hardware ID directly
        data = await websocket.receive_json()
        hardware_id = data.get("hardware_id")
        
        if not hardware_id:
            await websocket.close(code=1008, reason="Missing hardware_id")
            return
            
        # Register connection
        await mobile_manager.connect(websocket, hardware_id)
        await websocket.send_json({
            "type": "connection_status",
            "status": "connected", 
            "hardware_id": hardware_id
        })
        logger.info(f"Mobile monitoring hardware {hardware_id}")
        
        # Keep connection alive
        while True:
            try:
                # Wait for either data or timeout
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)
                # Update state from mobile and persist it
                if "event" in data and data["event"] == "update_appliances":
                    hardware_id = data.get("hardware_id")

                    state = await get_hardware_state(hardware_id)  # Ensure we have the latest state

                    if "appliance_counts" in data:
                        state["appliance_counts"] = data["appliance_counts"]
                        logger.info(f"🔧 Updated appliance_counts from mobile: {data['appliance_counts']}")

                    if "min_current" in data:
                        state["min_current"] = data["min_current"]
                        logger.info(f"🔧 Updated min_current from mobile: {data['min_current']}")
                    counts_received = True
                    logging.info(f"Received appliance counts: {data['appliance_counts']}")
                    
                    await save_hardware_state(hardware_id, state)
                    continue


                # Handle initialization message
                if "event" in data and data["event"] == "init_appliances":
                    hardware_id = data.get("hardware_id")

                    state = await get_hardware_state(hardware_id)

                    if not state or not hardware_id:
                        await websocket.send_json({"error": "Not authenticated"})
                        break

                    # Handle appliance_counts if provided, otherwise retain from loaded state
                    if "appliance_counts" in data:
                        state["appliance_counts"] = data["appliance_counts"]
                        logger.info(f"✅ appliance_counts received: {data['appliance_counts']}")
                    elif "appliance_counts" not in state or not state["appliance_counts"]:
                        # Optional: If no counts in state either, initialize to empty/default
                        state["appliance_counts"] = {}
                        logger.warning("⚠️ No appliance_counts provided or found in DB; initializing as empty.")
                    else:
                        logger.info("📦 Using appliance_counts from existing DB state.")
                    logging.info(f"Received appliance counts: {data['appliance_counts']}")

                    # Update min_current
                    state["min_current"] = data.get("min_current", state.get("min_current", 0.11))

                    await save_hardware_state(hardware_id, state)
                    logger.info(f"💾 Saved initialization state: {state}")

                    continue

                # Handle ping/pong
                if data.get("event") == "pong":
                    logger.debug(f"Received pong from {hardware_id}")
                    
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({
                        "type": "ping",
                        "timestamp": datetime.utcnow().isoformat()
                    })
                except:
                    break 
                    
            except WebSocketDisconnect:
                break
                
    except WebSocketDisconnect:
        logger.info("Mobile client disconnected")
    except Exception as e:
        logger.error(f"Mobile connection error: {str(e)}")
    finally:
        if hardware_id:
            mobile_manager.disconnect(websocket, hardware_id)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except RuntimeError:
                pass

# In your FastAPI code (db.py)
async def verify_hardware(hardware_id: str, api_key: str):
    hardware = await hardware_col.find_one({
        "hardware_id": hardware_id,
        "api_key": api_key
    })
    if not hardware:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return hardware

async def get_hardware_state(hardware_id: str):
    # Define default state structure
    default_state = {
        "hardware_id": hardware_id,
        "hourly_energy": {},
        "daily_energy": {},
        "monthly_energy": {},
        "daily_cost": {},
        "monthly_cost": {},
        "appliance_info": {},
        "top_appliances": [],
        "day_month_energy_cost": {},
        "appliance_durations": {},
        "appliance_states": {},
        "plugged_in_appliances": {},
        "unplugged_appliances": {},
        "appliance_power_energy": {},
        "last_current_values": {},
        "last_plug_in_times": {},
        "appliance_instances": {},
        "response_data": {},
        "daily_appliance_energy": {},
        "monthly_appliance_energy": {},
        "current_sequence": [],
        "appliance_counts":{},
        'tips':{}
    }

    # Get existing state from MongoDB
    state = await db.hardware_state.find_one({"hardware_id": hardware_id})
    
    if state:
        # Merge existing state with defaults (existing values take precedence)
        return {**default_state, **state}
    
    # Return fresh default state if no existing document
    return default_state
def convert_timestamps(data):
    """Recursively convert Timestamp objects to ISO strings"""
    if isinstance(data, (pd.Timestamp, datetime)):
        return data.isoformat()
    elif isinstance(data, dict):
        return {k: convert_timestamps(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_timestamps(item) for item in data]
    return data
async def save_hardware_state(hardware_id: str, state: dict):
    converted_state = convert_timestamps(state)

    await db.hardware_state.update_one(
        {"hardware_id": hardware_id},
        {"$set": converted_state},
        upsert=True
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    hardware_id = None
    model = None
    scaler = None
    label_encoder = None
    min_current = 0.11
    current_sequence = []
    state = None
    try:
        while True:
            data = await websocket.receive_json()
            logger.info(f"Received: {data}")

            state = await get_hardware_state(hardware_id)

            # Handle authentication message
            if "action" in data and data["action"] == "authenticate":
                try:
                    hardware_id = data["hardware_id"]
                    api_key = data["api_key"]
                    hardware = await verify_hardware(hardware_id, api_key)
                    # CORRECT: Registering with hardware manager
                    await hardware_manager.connect(websocket, hardware_id)
                    await websocket.send_json({"status": "authenticated"})
                    logger.info(f"Authenticated: {hardware_id}")
                                # Load hardware state
                    state = await get_hardware_state(hardware_id)
                    logger.info(f"Loaded state for {hardware_id}")
                    model = load_model('./cnn_lstm_appliance_classification_model.h5')
                    scaler = pickle.load(open('./scaler.pkl', 'rb'))
                    label_encoder = pickle.load(open('./label_encoder.pkl', 'rb'))
                    logger.info("🧠 ML models loaded successfully")
                except HTTPException as e:
                    await websocket.send_json({"error": e.detail})
                    break  # Break out of loop on auth failure
                continue
                                # Load ML models
            # Handle batch data
            if "current_values" in data:
                if not all([model, scaler, label_encoder, state]):
                    logger.warning("Processing requested before initialization complete")
                    await websocket.send_json({"error": "System not initialized"})
                    continue
                # Update current sequence
                current_sequence.extend(data["current_values"])
                # Keep only last 10 readings
                current_sequence = current_sequence[-40:]
                logger.debug(f"Updated current sequence: {current_sequence}")
                
                                # Load ML models

                # Process data
                result = await detect_appliances_real_time(
                    hardware_id,
                    current_sequence,
                    state,
                    model,
                    scaler,
                    label_encoder,
                    min_current=0.1,
                    interval=40,
                    voltage=220
                )
                
                # Update state and save
                state.update(result)
                await save_hardware_state(hardware_id, state)
                logger.info(f"Processing result: {json.dumps(result, indent=2, default=str)}")
                
                # Broadcast to the same hardware connection
                await mobile_manager.broadcast(hardware_id, result)
                
            await asyncio.sleep(1)  # Small sleep to prevent busy waiting

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {hardware_id}")
    except Exception as e:
        logger.error(f"Error with {hardware_id}: {str(e)}", exc_info=True)
        # Send error to client before closing
        if hardware_id and hardware_id in mobile_manager.active_connections:
            await mobile_manager.broadcast(hardware_id, {"error": str(e)})
    finally:
        if hardware_id:
            # Correct disconnect for hardware manager
            hardware_manager.disconnect(hardware_id)
        
        # Only close if still connected
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except RuntimeError:
                pass  # Already closed


async def process_hardware_data(data, hardware_id, state, model, scaler, label_encoder,min_current=0.11,interval=40,voltage=220):
    # Your existing processing logic modified to use the state parameter
    # Example modification:
    if "current_values" in data:
        current_sequence = state.get("current_sequence", [])
        current_sequence.extend(data["current_values"])
        
        if len(current_sequence) > 40:
            current_sequence = current_sequence[-40:]
        else:
            state["current_sequence"] = current_sequence
            
        state["current_sequence"] = current_sequence
        
        # Modified detect_appliances_real_time to use state parameter
        results = await detect_appliances_real_time(
            hardware_id,
            current_sequence,
            state,
            model,
            scaler,
            label_encoder,
            min_current=0.11,
            interval=40,
            voltage=220
        )
        
        return results


# Function to determine the slab rate for total energy consumption
async def calculate_cost(total_energy_wh):
    cost_slabs = [
        (50, .68),    # 0–50 kWh at 68 EGP per kWh
        (100, .78),   # 51–100 kWh at 78 EGP per kWh
        (200, .95),   # 101–200 kWh at 95 EGP per kWh
        (350, 1.55),  # 201–350 kWh at 155 EGP per kWh
        (650, 1.95),  # 351–650 kWh at 195 EGP per kWh
        (1000, 2.10), # 651–1000 kWh at 210 EGP per kWh
        (float('inf'), 2.50)  # Above 1000 kWh at 210 EGP per kWh (same rate as the previous)
    ]

    total_cost = 0
    previous_limit = 0
    for limit, rate in cost_slabs:
        if total_energy_wh > previous_limit:
            energy_in_slab = min(total_energy_wh, limit) - previous_limit
            total_cost += energy_in_slab * rate
            previous_limit = limit
        else:
            break
    return total_cost

from datetime import datetime, timedelta
appliance_durations={}
async def calculate_duration(hardware_id,plugged_in_appliances, unplugged_appliances, timestamps,state, interval=40):


    # Load previous durations from state

    for appliance_id, plug_in_times in plugged_in_appliances.items():
        if appliance_id not in appliance_durations:
            appliance_durations[appliance_id] = 0  # Initialize duration if not exists

        # Ensure plug-in times are datetime objects
       # Ensure plug-in times are datetime objects
        plug_in_times = [pd.to_datetime(t) for t in plug_in_times]
        last_plug_in_time = max(plug_in_times)

        # Ensure unplug times are datetime objects
        if appliance_id in unplugged_appliances:
            unplug_times = [pd.to_datetime(t) for t in unplugged_appliances[appliance_id]]
            last_unplug_time = max(unplug_times)
            
            if pd.to_datetime(last_unplug_time) > pd.to_datetime(last_plug_in_time):  # Convert to Timestamps
                # Calculate actual duration and skip live update
                duration_seconds = (last_unplug_time - last_plug_in_time).total_seconds()
                appliance_durations[appliance_id] += duration_seconds / 3600  # in hours
                continue

        # If still plugged in, increment by fixed interval
        appliance_durations[appliance_id] += interval / 3600  # Convert interval to hours
    state['appliance_durations']=appliance_durations
    # Save updated durations
    return appliance_durations

async def calculate_daily_monthly_energy(hardware_id,current_sequence, timestamps,state, voltage=220, interval=40):

    # Load previous values from state
    daily_energy = state.get("daily_energy", {})
    monthly_energy = state.get("monthly_energy", {})

    # Use string-based keys to avoid datetime object errors
    current_day = timestamps.strftime("%Y-%m-%d")
    current_month = timestamps.strftime("%Y-%m")

    # Ensure accumulators retain previous values
    day_energy_accumulator = daily_energy.get(current_day, 0)
    month_energy_accumulator = monthly_energy.get(current_month, 0)

    appliance_instance_start_times = {}  # To track the start times for appliances plugged in
    last_current = abs(current_sequence[-1])  # Use the last value in the sequence


    current = abs(last_current)
    power = current * voltage  # Power in watts
    energy_kwh = (power * interval) / 3600000  # Energy in kWh
    timestamp = timestamps  # Use the provided timestamp, not datetime.now()
    day_str = timestamp.strftime("%Y-%m-%d")
    month_str = timestamp.strftime("%Y-%m")
        # Update the energy accumulation
    day_energy_accumulator += energy_kwh
    month_energy_accumulator += energy_kwh
        # Track the start time of the appliance plug-in event
    appliance_instance_start_times[timestamp] = current

        # If a new day starts, save the previous day's energy and reset accumulator
    if day_str != current_day:
        daily_energy[current_day] = day_energy_accumulator
        current_day = day_str
        day_energy_accumulator = daily_energy.get(current_day, 0)  # Carry forward previous value if exists

        # If a new month starts, save the previous month's energy and reset accumulator
    if month_str != current_month:
        monthly_energy[current_month] = month_energy_accumulator
        current_month = month_str
        month_energy_accumulator = monthly_energy.get(current_month, 0)  # Carry forward previous value if exists

    # Store the last day's and month's energy
    daily_energy[current_day] = day_energy_accumulator
    monthly_energy[current_month] = month_energy_accumulator

    # Update state and save
    state["daily_energy"] = daily_energy
    state["monthly_energy"] = monthly_energy
   
    return daily_energy, monthly_energy

async def get_top_5_consuming_appliances(daily_appliance_energy, monthly_appliance_energy):
    # Sorting the daily energy consumption
    top_consuming_appliances_day = []
    for day, appliances in daily_appliance_energy.items():
        sorted_appliances = sorted(appliances.items(), key=lambda x: x[1], reverse=True)
        for appliance, energy in sorted_appliances[:5]:  # Get top 5 appliances
            top_consuming_appliances_day.append({
                "appliance": appliance,
                "energy_consumed_kWh": energy / 1000,  # Convert Wh to kWh
                "day": str(day)
            })

    # Sorting the monthly energy consumption
    top_consuming_appliances_month = []
    for month, appliances in monthly_appliance_energy.items():
        sorted_appliances = sorted(appliances.items(), key=lambda x: x[1], reverse=True)
        for appliance, energy in sorted_appliances[:5]:  # Get top 5 appliances
            top_consuming_appliances_month.append({
                "appliance": appliance,
                "energy_consumed_kWh": energy / 1000,  # Convert Wh to kWh
                "month": str(month)
            })

    return top_consuming_appliances_day, top_consuming_appliances_month

async def get_top_consuming_appliances_for_currentday(hardware_id, daily_appliance_energy, top_n=4):
    state = await get_hardware_state(hardware_id)
    top_appliances = []
    current_day = pd.Timestamp.today().date()

    try:
        daily_energy = state.get("daily_appliance_energy", {})
        daily_energy_data = daily_energy.get(str(current_day), {})

        # Validate energy values
        validated_energy = {}
        for appliance_id, energy in daily_energy_data.items():
            if isinstance(energy, dict):
                # Sum values if stored as time-energy pairs
                validated_energy[appliance_id] = sum(float(v) for v in energy.values())
            else:
                validated_energy[appliance_id] = float(energy)

        sorted_appliances = sorted(validated_energy.items(), key=lambda x: x[1], reverse=True)

        for appliance_id, energy in sorted_appliances[:top_n]:
            energy_kwh = energy / 1000
            cost = await calculate_cost(energy_kwh)
            
            top_appliances.append({
                "instance": appliance_id,
                "energy_consumed_kWh": round(energy_kwh, 2),
                "energy_cost": round(float(cost), 2)  # Explicit float conversion
            })

    except Exception as e:
        logger.error(f"Error in top appliances: {str(e)}")
    
    return top_appliances

import random 
import csv
import re



async def normalize_appliance_name(appliance_name: str) -> str:
        return re.sub(r'_\d+$', '', appliance_name)  # Remove any trailing "_number"

async def get_top_5_tips(hardware_id,daily_appliance_energy, appliance_tips_dict):
        top_5_appliances = await get_top_consuming_appliances_for_currentday(hardware_id,daily_appliance_energy)
        tips_list = []
        for appliance in top_5_appliances:
            # Normalize appliance name to remove instance number (e.g., "Fan_1" -> "Fan")
            appliance_name = await normalize_appliance_name(appliance['instance'])
            if appliance_name in appliance_tips_dict:
                tips = appliance_tips_dict[appliance_name]  # Get tips for the normalized appliance name
                random.shuffle(tips)  # Shuffle tips for the appliance
                tips_list.append({"appliance": appliance['instance'], "tip": tips[0]})  # Store one random tip
        return tips_list
 # Energy per appliance instance for each month
async def notify_mobile_users(user_id: str, data: dict):
    # Implement mobile notification logic here
    pass



# Add user management endpoints
@app.post("/register")
async def register_hardware(hardware_id: str, api_key: str, user_id: str):
    hardware_col.insert_one({
        "hardware_id": hardware_id,
        "api_key": api_key,
        "user_id": user_id,
        "registered_at": datetime.now()
    })
    return {"status": "registered"}
@app.get("/data/{hardware_id}")
async def get_hardware_data(hardware_id: str, user_id: str):
    hardware = await hardware_col.find_one({"hardware_id": hardware_id})  # Add await
    state = await get_hardware_state(hardware_id)  # Add await
    if not hardware or hardware['user_id'] != user_id:
        raise HTTPException(403, "Unauthorized access")
    
    return {
        "hourly_energy": state.get("hourly_energy", {}),
        "daily_energy": state.get("daily_energy", {}),
        "monthly_energy": state.get("monthly_energy", {}),
        "daily_cost": state.get("daily_cost", {}),
        "monthly_cost": state.get("monthly_cost", {}),
        "appliance_info": state.get("appliance_info", {}),
        "appliance_durations": state.get("appliance_durations", {}),
        "appliance_states": state.get("appliance_states", {}),
        "appliance_power_energy": state.get("appliance_power_energy", {}),
        "plugged_in_appliances": state.get("plugged_in_appliances", {}),
        "unplugged_appliances": state.get("unplugged_appliances", {}),
        "last_current_values": state.get("last_current_values", {}),
        "last_plug_in_times": state.get("last_plug_in_times", {}),
        "top_appliances": state.get("top_appliances", []),  # Empty list for array
        "day_month_energy_cost": state.get("day_month_energy_cost", {}),
        "daily_appliance_energy": state.get("daily_appliance_energy", {}),
        "monthly_appliance_energy": state.get("monthly_appliance_energy", {}),
        "appliance_instances": state.get("appliance_instances", {}),
        "response_data": state.get("response_data", {}),
        "appliance_counts": state.get("appliance_counts", {}),
        "tips":state.get("tips", {})
    }

# Modified detection function signature
async def detect_appliances_real_time(hardware_id,current_sequence, state, model, scaler, label_encoder, min_current, interval=40, voltage=220, top_n=100):
    current_time = pd.Timestamp.now()
    threshold = 0.12
    # 2. Create local references (consistent with your pattern)
    hourly_energy = state.get('hourly_energy', {})
    daily_energy = state.get('daily_energy', {})
    monthly_energy = state.get('monthly_energy', {})
    daily_cost = state.get('daily_cost', {})
    monthly_cost = state.get('monthly_cost', {})
    appliance_info = state.get('appliance_info', {})
    appliance_durations = state.get('appliance_durations', {})
    appliance_states = state.get('appliance_states', {})
    appliance_power_energy = state.get('appliance_power_energy', {})
    plugged_in_appliances = state.get('plugged_in_appliances', {})
    unplugged_appliances = state.get('unplugged_appliances', {})
    last_current_values = state.get('last_current_values', {})
    last_plug_in_times = state.get('last_plug_in_times', {})
    top_appliances = state.get('top_appliances', [])
    day_month_energy_cost = state.get('day_month_energy_cost', {})
    daily_appliance_energy = state.get('daily_appliance_energy', {})
    monthly_appliance_energy = state.get('monthly_appliance_energy', {})
    appliance_instances = state.get('appliance_instances', {})
    response_data = state.get('response_data', {})
    appliance_counts= state.get("appliance_counts", {})
    tips=state.get("tips", {})


    last_month = None  # To track the last month (for reset)

    total_energy_consumption = 0  # Initialize total energy consumption

    current_reading = current_sequence[-20:]
    current_diff = np.array(current_sequence[-20:]) - np.array(current_sequence[:20])
    avgcurr_diff = np.mean(current_diff)  # Proper mean of differences
    hourly_energy = await calculate_hourly_energy(hardware_id, np.mean(current_reading), pd.Timestamp.now())

    if np.mean(current_reading) < 0.12:
        for instance_id in list(plugged_in_appliances.keys()):
            if instance_id not in unplugged_appliances:
                unplugged_appliances[instance_id] = []
            unplugged_appliances[instance_id].append(current_time)
            
            appliance_states[instance_id] = 'plugged out'
            last_current_values.pop(instance_id, None)
            last_plug_in_times.pop(instance_id, None)
            plugged_in_appliances.pop(instance_id)
            
            base_appliance = instance_id.split('_')[0]
            # Use state directly instead of appliance_instance_counts
            if base_appliance in state['appliance_instances']:
                # Decrement count and prevent negative values
                state['appliance_instances'][base_appliance] = max(
                    0, 
                    state['appliance_instances'][base_appliance] - 1  
                )

        
        # Return early instead of continue

    current_value_scaled = scaler.transform(np.array([abs(current_diff)]).reshape(-1, 1))  # Reshape to (5, 1)
    current_value_scaled = current_value_scaled.reshape(1, 20, 1)
    predictions = model.predict(current_value_scaled)
    predicted_labels = np.argsort(predictions[0])[-top_n:][::-1]


    appliance_power = abs(avgcurr_diff) * voltage
    appliance_energy = appliance_power * (interval / 3600)  # Energy in Wh

    total_energy_consumption += appliance_energy

    current_month = current_time.month

    if current_month != last_month:
        if last_month is not None:
            # Reset the monthly energy tracking at the beginning of the new month
            monthly_appliance_energy = {}
        last_month = current_month

    current_day = current_time.date()
    appliance_instances = state['appliance_instances']


    if avgcurr_diff > threshold:  # Plug-in event
        for predicted_label in predicted_labels:
            predicted_appliance = label_encoder.inverse_transform([predicted_label])[0]
            if predicted_appliance not in appliance_counts:
                continue

            # Get current count FROM state (default to 0 if missing)
            current_count = state['appliance_instances'].get(predicted_appliance, 0)

            if current_count < appliance_counts[predicted_appliance]:
                # Generate instance ID (e.g., "Fan_1" when current_count=0)
                instance_id = f"{predicted_appliance}_{current_count + 1}"

                # Update state count
                state['appliance_instances'][predicted_appliance] = current_count + 1

                # Register the new instance
                if instance_id not in appliance_states or appliance_states[instance_id] == 'plugged out':
                    plugged_in_appliances.setdefault(instance_id, []).append(current_time)
                    appliance_power_energy.setdefault(instance_id, {'power': appliance_power, 'energy': 0})
                    appliance_power_energy[instance_id]['power'] = appliance_power  # <-- always update

                    appliance_power_energy[instance_id]['energy'] += appliance_energy
                    appliance_states[instance_id] = 'plugged in'
                    last_current_values[instance_id] = abs(max(current_diff))
                    last_plug_in_times[instance_id] = current_time

                    break  # Only create one instance per detection cycle

    elif avgcurr_diff < -threshold:  # Plug-out event
        unplugged_combo = None
        possible_combos = []

        for instance_id, appliance_state  in appliance_states.items():
            if appliance_state  == 'plugged in':
                possible_combos.append(instance_id)

        for r in range(1, len(possible_combos) + 1):
            for combo in combinations(possible_combos, r):
                total_current = sum(last_current_values[appliance] for appliance in combo)
                if abs(total_current - abs(max(current_diff))) < threshold:
                    unplugged_combo = combo
                    break
            if unplugged_combo:
                break

        if unplugged_combo:
            for instance_id in unplugged_combo:
                if instance_id not in unplugged_appliances:
                    unplugged_appliances[instance_id] = []
                unplugged_appliances[instance_id].append(current_time)

                appliance_states[instance_id] = 'plugged out'
                last_current_values.pop(instance_id, None)

                base_appliance = instance_id.split('_')[0]
                if base_appliance in state['appliance_instances']:
                # Decrement count in state directly
                    state['appliance_instances'][base_appliance] = max(
                        0, state['appliance_instances'][base_appliance] - 1
                    )
    else:
        pass  # no significant event



    appliance_durations = await calculate_duration(hardware_id, plugged_in_appliances, unplugged_appliances, current_time,state, interval)

    # Now calculate daily and monthly energy
    daily_energy, monthly_energy = await calculate_daily_monthly_energy(hardware_id,
        current_sequence,
        current_time,state,
        voltage,
        interval
    )
    daily_cost = {day: await calculate_cost(energy) for day, energy in daily_energy.items()}
    monthly_cost = {month: await calculate_cost(energy) for month, energy in monthly_energy.items()}

    appliance_energies = {}
    appliance_costs = {}
    for instance_id, duration in appliance_durations.items():
        if instance_id not in appliance_power_energy:
            appliance_power_energy[instance_id] = {'power': 0, 'energy': 0}
        appliance_power = appliance_power_energy[instance_id]['power']
        appliance_energy = round(appliance_power * duration,3)
        appliance_energies[instance_id] = appliance_energy

        # Daily and monthly energy tracking
        appliance_day = current_time.date()
        if appliance_day not in daily_appliance_energy:
            daily_appliance_energy[appliance_day] = {}
        if instance_id not in daily_appliance_energy[appliance_day]:
            daily_appliance_energy[appliance_day][instance_id] = 0
        daily_appliance_energy[appliance_day][instance_id] += appliance_energy

        appliance_month = current_time.month
        if appliance_month not in monthly_appliance_energy:
            monthly_appliance_energy[appliance_month] = {}
        if instance_id not in monthly_appliance_energy[appliance_month]:
            monthly_appliance_energy[appliance_month][instance_id] = 0
        monthly_appliance_energy[appliance_month][instance_id] += appliance_energy

    top_consuming_appliances_day = {}
    top_consuming_appliances_month = {}

    # Calculate top-consuming appliances by instance for each day
    for day, appliances in daily_appliance_energy.items():
        sorted_appliances_day = sorted(appliances.items(), key=lambda x: x[1], reverse=True)
        top_consuming_appliances_day[day] = [{"instance": appliance[0], "energy_consumed_kWh": appliance[1] / 1000}
                                            for appliance in sorted_appliances_day[:top_n]]

    # Calculate top-consuming appliances by instance for each month
    for month, appliances in monthly_appliance_energy.items():
        sorted_appliances_month = sorted(appliances.items(), key=lambda x: x[1], reverse=True)
        top_consuming_appliances_month[month] = [{"instance": appliance[0], "energy_consumed_kWh": appliance[1] / 1000}
                                                for appliance in sorted_appliances_month[:top_n]]

    top_consuming_appliances_currentday = await get_top_consuming_appliances_for_currentday(hardware_id,daily_appliance_energy, top_n)
    day_month_energy_cost = await get_day_month_energy_cost(hardware_id,daily_energy, monthly_energy, daily_cost, monthly_cost)


    def load_appliance_tips_from_csv(file_path):
        appliance_tips = {}
        try:
            # Open CSV file with proper handling of BOM (Byte Order Mark)
            with open(file_path, 'r', encoding='utf-8-sig') as csv_file:  # 'utf-8-sig' handles BOM
                reader = csv.DictReader(csv_file)
                fieldnames = reader.fieldnames
                logging.debug(f"CSV Columns: {fieldnames}")  # Log column names for debugging
                
                # Check if the required columns exist
                if 'appliance' not in fieldnames or 'tip' not in fieldnames:
                    logging.error("Missing required columns in the CSV file.")
                    raise ValueError("CSV file must contain 'appliance' and 'tip' columns.")
                
                for row in reader:
                    appliance = row['appliance']
                    tips = row['tip'].split(';')  # Assuming tips are separated by ';'
                    if appliance not in appliance_tips:
                        appliance_tips[appliance] = []
                    appliance_tips[appliance].extend(tips)
        except Exception as e:
            logging.error(f"Error loading CSV: {e}")
            raise ValueError(f"Error loading appliance tips CSV: {e}")
        
        return appliance_tips

    appliance_tips_file = './app.csv'
    appliance_tips_dict = load_appliance_tips_from_csv(appliance_tips_file)
    tips = await get_top_5_tips(hardware_id, state['daily_appliance_energy'], appliance_tips_dict)
    state['tips']=tips


    for instance_id, duration in appliance_durations.items():
        appliance_power = appliance_power_energy[instance_id]['power']
        appliance_energy = appliance_power * duration/1000
        appliance_cost = await calculate_cost(appliance_energy)
        appliance_energies[instance_id] = appliance_energy
        appliance_costs[instance_id] = round(appliance_cost, 2)

        # Determine the current appliance status
        plug_times = plugged_in_appliances.get(instance_id, [])
        unplug_times = unplugged_appliances.get(instance_id, [])

        # Convert times to datetime, handling potential None values
        plug_times = [pd.to_datetime(t) for t in plug_times if t is not None]
        unplug_times = [pd.to_datetime(t) for t in unplug_times if t is not None]

        # Determine last plug-in and unplug times
        last_plugged_in = max(plug_times, default=None)
        last_unplugged = max(unplug_times, default=None)

        if last_plugged_in is None and last_unplugged is None:
            status = "unknown"  # If never plugged in or unplugged
        elif last_plugged_in and (last_unplugged is None or last_plugged_in > last_unplugged):
            status = "plugged in"
        else:
            status = "plugged out"

        current_t = datetime.now().isoformat()

        # Ensure monthly energy is accumulated correctly in kWh
        appliance_info[instance_id] = {
            "instance_id": instance_id,
            "status": status,
            "monthly_energy_consumed": round(monthly_appliance_energy.get(current_time.month, {}).get(instance_id, 0) / 1000, 3),
            "monthly_power_usage": round(appliance_power / 1000, 2),
            "monthly_cost": round(appliance_cost, 3),
            "duration in minutes": round(duration * 60, 3),
            "last_updated": current_t  # Capture the current time once
        }
    state["appliance_info"] = appliance_info

    response_data = {
        "plugged_in_appliances": {
            instance_id: [t.isoformat() if hasattr(t, 'isoformat') else str(t) for t in times]
            for instance_id, times in plugged_in_appliances.items()
        },
        "unplugged_appliances": {
            instance_id: [t.isoformat() if hasattr(t, 'isoformat') else str(t) for t in times]
            for instance_id, times in unplugged_appliances.items()
        },
        "total_energy_consumption": total_energy_consumption,
        "appliance_energies": appliance_energies,
        "appliance_costs": appliance_costs,
        "daily_energy": {str(day): energy for day, energy in daily_energy.items()},  # Convert date to string
        "monthly_energy": {str(month): energy for month, energy in monthly_energy.items()},  # Convert month to string
        "daily_cost": {str(day): cost for day, cost in daily_cost.items()},  # Convert date to string
        "monthly_cost": {str(month): cost for month, cost in monthly_cost.items()},  # Convert month to string
        "top_consuming_appliances_day": {str(day): [{'instance': appliance['instance'], 'energy_consumed_kWh': appliance['energy_consumed_kWh']} for appliance in appliances] for day, appliances in top_consuming_appliances_day.items()},
        "top_consuming_appliances_month": {str(month): [{'instance': appliance['instance'], 'energy_consumed_kWh': appliance['energy_consumed_kWh']} for appliance in appliances] for month, appliances in top_consuming_appliances_month.items()},
        "top_consuming_appliances_currentday": [{'instance': appliance['instance'], 'energy_consumed_kWh': round(appliance['energy_consumed_kWh'],2), 'energy_cost': round(appliance['energy_cost'],2)} for appliance in top_consuming_appliances_currentday],
        "tips": tips,
        "hourly_energy": hourly_energy,  # Include hourly energy data in the response
        "day_month_energy_cost":day_month_energy_cost,

        "appliance_info": {
            instance_id: {
                "instance_id": instance_id,
                "status": info['status'],
                "monthly_energy_consumed": info['monthly_energy_consumed'],
                "monthly_power_usage": info['monthly_power_usage'],
                "monthly_cost": info['monthly_cost'],
                "duration in minutes": info['duration in minutes'],
                "last_updated": info['last_updated']  # Assuming this is already in ISO format
            }
            for instance_id, info in appliance_info.items()
        }
    }
    state['daily_energy'] = {str(day): energy for day, energy in daily_energy.items()}  # Convert date keys
    state['monthly_energy'] = {str(month): energy for month, energy in monthly_energy.items()}  # Convert month keys
    state['daily_cost'] = {str(day): cost for day, cost in daily_cost.items()}  # Convert date keys
    state['monthly_cost'] = {str(month): cost for month, cost in monthly_cost.items()}  # Convert month keys
    state['hourly_energy'] = hourly_energy
    state['appliance_info'] = appliance_info
    state['appliance_states'] = appliance_states  # Direct assignment
    state['appliance_power_energy'] = appliance_power_energy  # Direct assignment
    state['plugged_in_appliances'] = plugged_in_appliances  # Direct assignment
    state['unplugged_appliances'] = unplugged_appliances  # Direct assignment
    state['last_current_values'] = last_current_values  # Direct assignment
    state['last_plug_in_times'] = last_plug_in_times  # Di
    state['appliance_instances'] = appliance_instances  # Direct assignment
    state['response_data'] = response_data
    state['tips'] = tips

    state['top_appliances'] = [
        {**appliance, 'energy_cost': round(appliance['energy_cost'], 2)}
        for appliance in top_consuming_appliances_currentday  # Clean list data
    ]

    state['day_month_energy_cost'] = {
        k: round(v, 2) if isinstance(v, (int, float)) else v
        for k, v in day_month_energy_cost.items()  # Clean numerical values
    }

    state['daily_appliance_energy'] = {
        str(day): {k: round(v, 3) for k, v in appliances.items()}
        for day, appliances in daily_appliance_energy.items()  # String dates
    }

    state['monthly_appliance_energy'] = {
        str(month): {k: round(v, 3) for k, v in appliances.items()}
        for month, appliances in monthly_appliance_energy.items()  # String months
    }
    await save_hardware_state(hardware_id, state)
    return{
    **response_data,
    # Explicitly include all state fields
    "appliance_info": appliance_info,
    "appliance_durations": appliance_durations,
    "appliance_states": appliance_states,
    "appliance_power_energy": appliance_power_energy,
    "plugged_in_appliances": plugged_in_appliances,
    "unplugged_appliances": unplugged_appliances,
    "last_current_values": last_current_values,
    "last_plug_in_times": last_plug_in_times,
    "appliance_instances": appliance_instances,
    "tips":tips
}

async def calculate_hourly_energy(hardware_id,current_value, timestamp):
    state = await get_hardware_state(hardware_id)

    if 'hourly_energy' not in state:
        state['hourly_energy'] = {}

    current_time = pd.Timestamp.now()
    hour_key = current_time.floor('H').isoformat()

    voltage = 220
    power = current_value * voltage
    energy_kwh = round((power / 1000) * (40 / 3600), 4)

    # Update current hour energy
    state['hourly_energy'][hour_key] = state['hourly_energy'].get(hour_key, 0) + energy_kwh

    # Cleanup: Convert all keys to Timestamps before comparing
    cutoff = current_time - timedelta(hours=24)
    cleaned_hourly_energy = {}

    for key_str, val in state['hourly_energy'].items():
        try:
            key_timestamp = pd.to_datetime(key_str)
            if key_timestamp >= cutoff:
                cleaned_hourly_energy[key_str] = val
        except Exception as e:
            print(f"Error parsing timestamp key {key_str}: {e}")
            continue

    state['hourly_energy'] = cleaned_hourly_energy

    await save_hardware_state(hardware_id, state)

    return state['hourly_energy']


async def get_day_month_energy_cost(hardware_id, daily_energy, monthly_energy, daily_cost, monthly_cost):
    state = await get_hardware_state(hardware_id)
    
    current_day = datetime.today().strftime("%Y-%m-%d")
    current_month = datetime.today().strftime("%Y-%m")
    
    # Use the passed daily_energy and monthly_energy which contain totals
    energy_cost_data = {
        "current_day_energy": round(daily_energy.get(current_day, 0), 2),
        "current_day_cost": round(daily_cost.get(current_day, 0), 2),
        "current_month_energy": round(monthly_energy.get(current_month, 0), 2),
        "current_month_cost": round(monthly_cost.get(current_month, 0), 2),
    }

    state["day_month_energy_cost"] = energy_cost_data
    await save_hardware_state(hardware_id, state)
    return energy_cost_data
