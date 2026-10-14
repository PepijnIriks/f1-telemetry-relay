"""F1 22 UDP packet formats (little endian) and the names behind the game's numbers."""
import struct

# ----------------------------------------------------------------- F1 22 packet formats (little endian)
HEADER = struct.Struct("<HBBBBQfIBB")                      # 24 bytes
HEADER_FIELDS = ("format", "major", "minor", "version", "packet_id",
                 "session_uid", "session_time", "frame", "player", "player2")
TEL_STRUCT = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")       # 60 bytes per car (CarTelemetry)
LAP_STRUCT = struct.Struct("<IIHHfff14BHHB")               # 43 bytes per car (LapData)
STATUS_STRUCT = struct.Struct("<BBBBBfffHHBBHBBBbfBfffB")  # 47 bytes per car (CarStatus)
DAMAGE_STRUCT = struct.Struct("<4f26B")                    # 42 bytes per car (CarDamage)
MOTION_SIZE = 60                                           # bytes per car (Motion)
assert (HEADER.size, TEL_STRUCT.size, LAP_STRUCT.size, STATUS_STRUCT.size, DAMAGE_STRUCT.size) == (24, 60, 43, 47, 42)

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants",
    5: "CarSetups", 6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification",
    9: "LobbyInfo", 10: "CarDamage", 11: "SessionHistory",
}
TRACKS = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir (Bahrain)", 4: "Barcelona",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Texas",
    16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico", 20: "Baku",
    21: "Sakhir Short", 22: "Silverstone Short", 23: "Texas Short", 24: "Suzuka Short",
    25: "Hanoi", 26: "Zandvoort", 27: "Imola", 28: "Portimao", 29: "Jeddah", 30: "Miami",
}
SESSION_TYPES = {
    0: "Unknown", 1: "Practice 1", 2: "Practice 2", 3: "Practice 3",
    4: "Short practice", 5: "Qualifying 1", 6: "Qualifying 2", 7: "Qualifying 3",
    8: "Short qualifying", 9: "One-shot qualifying", 10: "Race", 11: "Race 2",
    12: "Race 3", 13: "Time trial",
}
WEATHER = {0: "Clear", 1: "Light cloud", 2: "Overcast", 3: "Light rain",
           4: "Heavy rain", 5: "Storm"}
COMPOUNDS = {16: "Soft", 17: "Medium", 18: "Hard", 7: "Inter", 8: "Wet"}
