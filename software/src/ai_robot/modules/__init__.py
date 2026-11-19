# SPDX-License-Identifier: MIT
"""Modules: what the robot does.

Each module owns some ports and talks to the others only through the messages in
``ai_robot.messages``. A module starts as a single file; when it grows, turn the file
into a package (``voice/__init__.py`` importing the class) and the entry below stays
the same.
"""

# Module name, as used in [modules] of the configuration -> "package.module:Class"
MODULES: dict[str, str] = {
    "behavior": "ai_robot.modules.behavior:BehaviorModule",
    "cloud": "ai_robot.modules.cloud:CloudModule",
    "motion": "ai_robot.modules.motion:MotionModule",
    "reset": "ai_robot.modules.reset:ResetModule",
    "screen": "ai_robot.modules.screen:ScreenModule",
    "settings": "ai_robot.modules.settings:SettingsModule",
    "setup": "ai_robot.modules.setup:SetupModule",
    "system": "ai_robot.modules.system:SystemModule",
    "vision": "ai_robot.modules.vision:VisionModule",
    "voice": "ai_robot.modules.voice:VoiceModule",
}
