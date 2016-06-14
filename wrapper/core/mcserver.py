# -*- coding: utf-8 -*-

from utils.helpers import getargs, getargsafter, processcolorcodes
from api.base import API
from api.player import Player
from api.world import World

from core.backups import Backups
from core.exceptions import UnsupportedOSException, InvalidServerStateError

import time
import threading
import subprocess
import os
import json
import ctypes
import platform
import base64

# Py3-2
import sys
PY3 = sys.version_info > (3,)

try:
    import resource
except ImportError:
    resource = False


# noinspection PyBroadException,PyUnusedLocal
class MCServer:

    def __init__(self, wrapper):
        self.log = wrapper.log
        self.config = wrapper.config
        self.encoding = self.config["General"]["encoding"]
        self.serverpath = self.config["General"]["server-directory"]
        self.wrapper = wrapper
        commargs = self.config["General"]["command"].split(" ")
        self.args = []

        for part in commargs:
            if part[-4:] == ".jar":
                self.args.append("%s/%s" % (self.serverpath, part))
            else:
                self.args.append(part)

        self.api = API(wrapper, "Server", internal=True)
        self.backups = Backups(wrapper)

        if "serverState" not in self.wrapper.storage:
            self.wrapper.storage["serverState"] = True

        self.players = {}
        self.state = MCSState.OFF
        self.bootTime = time.time()
        self.boot = self.wrapper.storage["serverState"]
        self.proc = None
        self.rebootWarnings = 0
        self.lastsizepoll = 0
        self.data = []

        if not self.wrapper.storage["serverState"]:
            self.log.warning("NOTE: Server was in 'STOP' state last time Wrapper.py was running. "
                             "To start the server, run /start.")
            time.sleep(5)

        # Server Information
        self.worldname = None
        self.worldSize = 0
        self.maxPlayers = 20
        self.protocolVersion = -1  # -1 until proxy mode checks the server's MOTD on boot
        self.version = None  # this is string name of the server version.
        self.world = None
        self.motd = None
        self.timeofday = -1  # -1 until a player logs on and server sends a time update
        self.onlineMode = True
        self.serverIcon = None

        self.properties = {}

        # has to be done immediately to get worldname; otherwise a "None" folder gets created in the server folder.
        self.reloadproperties()  # This will be redone on server start

        if self.config["General"]["timed-reboot"] or self.config["Web"]["web-enabled"]:  # don't reg. an unused event
            self.api.registerEvent("timer.second", self.eachsecond)

    def init(self):
        """
        Start up the listen threads for reading server console output
        """
        capturethread = threading.Thread(target=self.__stdout__, args=())
        capturethread.daemon = True
        capturethread.start()

        capturethread = threading.Thread(target=self.__stderr__, args=())
        capturethread.daemon = True
        capturethread.start()

    def __del__(self):
        self.state = 0  # MCSState.OFF use hard-coded number in case Class MCSState is GC'ed

    def __handle_server__(self):
        """
        Internally-used function that handles booting the server, parsing console output, and etc.
        """
        trystart = 0
        while not self.wrapper.halt:
            trystart += 1
            self.proc = None
            if not self.boot:
                time.sleep(0.1)
                continue
            self.changestate(MCSState.STARTING)
            self.log.info("Starting server...")
            self.reloadproperties()
            self.proc = subprocess.Popen(self.args, cwd=self.serverpath, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.PIPE, universal_newlines=True)
            self.players = {}
            self.accepteula()  # Auto accept eula
            if self.proc.poll() is None and trystart > 3:
                self.log.error("could not start server.  check your server.properties, wrapper.properties and this"
                               " startup 'command' from wrapper.properties:\n'%s'", " ".join(self.args))
                self.wrapper.halt = True

            # The server loop
            while True:
                time.sleep(0.1)
                if self.proc.poll() is not None:
                    self.changestate(MCSState.OFF)
                    if not self.config["General"]["auto-restart"]:
                        self.wrapper.halt = True
                    self.log.info("Server stopped")
                    break
                for line in self.data:
                    try:
                        self.readconsole(line.replace("\r", ""))
                    except Exception as e:
                        self.log.exception(e)
                self.data = []

    def start(self, save=True):
        """
        Start the Minecraft server
        """
        self.boot = True
        if save:
            self.wrapper.storage["serverState"] = True

    def restart(self, reason="Restarting Server"):
        """
        Restart the Minecraft server, and kick people with the specified reason
        """
        self.log.info("Restarting Minecraft server with reason: %s", reason)
        self.changestate(MCSState.STOPPING, reason)
        for player in self.players:
            self.console("kick %s %s" % (player, reason))
        self.console("stop")

    def accepteula(self):
        if os.path.isfile("%s/eula.txt" % self.serverpath):
            self.log.debug("Checking EULA agreement...")
            with open("%s/eula.txt" % self.serverpath, "r") as f:
                eula = f.read()

            if "false" in eula:
                # if forced, should be at info level since acceptance is a legal matter.
                self.log.warning("EULA agreement was not accepted, accepting on your behalf...")
                with open("%s/eula.txt" % self.serverpath, "w") as f:
                    f.write(eula.replace("false", "true"))

            self.log.debug("EULA agreement has been accepted.")
            return True
        else:
            return False

    def stop(self, reason="Stopping Server", save=True):
        """
        Stop the Minecraft server, prevent it from auto-restarting and kick people with the specified reason
        """
        self.log.info("Stopping Minecraft server with reason: %s", reason)
        self.changestate(MCSState.STOPPING, reason)
        self.boot = False
        if save:
            self.wrapper.storage["serverState"] = False
        for player in self.players:
            self.console("kick %s %s" % (player, reason))
        self.console("stop")

    def kill(self, reason="Killing Server"):
        """ 
        Forcefully kill the server. It will auto-restart if set in the configuration file
        """
        self.log.info("Killing Minecraft server with reason: %s", reason)
        self.changestate(MCSState.OFF, reason)
        self.proc.kill()

    def freeze(self, reason="Server is now frozen. You may disconnect momentarily."):
        """ 
        Freeze the server with `kill -STOP`. Can be used to stop the server in an emergency without shutting it down, 
        so it doesn't write corrupted data - e.g. if the disk is full, you can freeze the server, free up some disk
        space, and then unfreeze
        'reason' argument is printed in the chat for all currently-connected players, unless you specify None.
        This command currently only works for *NIX based systems
        """
        if self.state != MCSState.OFF:
            if os.name == "posix":
                self.log.info("Freezing server with reason: %s", reason)
                self.broadcast("&c%s" % reason)
                time.sleep(0.5)
                self.changestate(MCSState.FROZEN)
                os.system("kill -STOP %d" % self.proc.pid)
            else:
                raise UnsupportedOSException("Your current OS (%s) does not support this command at this time."
                                             % os.name)
        else:
            raise InvalidServerStateError("Server is not started. You may run '/start' to boot it up.")

    def unfreeze(self):
        """
        Unfreeze the server with `kill -CONT`. Counterpart to .freeze(reason)
        This command currently only works for *NIX based systems
        """
        if self.state != MCSState.OFF:
            if os.name == "posix":
                self.log.info("Unfreezing server...")
                self.broadcast("&aServer unfrozen.")
                self.changestate(MCSState.STARTED)
                os.system("kill -CONT %d" % self.proc.pid)
            else:
                raise UnsupportedOSException("Your current OS (%s) does not support this command at this time."
                                             % os.name)
        else:
            raise InvalidServerStateError("Server is not started. Please run '/start' to boot it up.")

    def broadcast(self, message=""):
        """
        Broadcasts the specified message to all clients connected. message can be a JSON chat object, 
        or a string with formatting codes using the & as a prefix 
        """
        if isinstance(message, dict):
            if self.config["General"]["pre-1.7-mode"]:
                self.console("say %s" % self.chattocolorcodes(message))
            else:
                self.console("tellraw @a %s" % json.dumps(message, encoding=self.encoding, ensure_ascii=False))
        else:
            if self.config["General"]["pre-1.7-mode"]:
                self.console("say %s" %
                             self.chattocolorcodes(json.loads(processcolorcodes(message)).decode(self.encoding)))
            else:
                self.console("tellraw @a %s" % processcolorcodes(message))

    def chattocolorcodes(self, jsondata):
        def getcolorcode(color):
            for code in API.colorcodes:
                if API.colorcodes[code] == color:
                    return "\xa7\xc2" + code
            return ""

        def handlechunk(chunk):
            extras = ""
            if "color" in chunk:
                extras += getcolorcode(chunk["color"])
            if "text" in chunk:
                extras += chunk["text"]
            if "string" in chunk:
                extras += chunk["string"]
            return extras

        total = handlechunk(jsondata)

        if "extra" in jsondata:
            for extra in jsondata["extra"]:
                total += handlechunk(extra)
        return total.encode(self.encoding)

    def login(self, username, eid, location):
        """
        Called when a player logs in
        """
        if username not in self.players:
            self.players[username] = Player(username, self.wrapper)
        if self.wrapper.proxy:
            playerclient = self.getplayer(username).getClient()
            playerclient.servereid = eid
            playerclient.position = location

        self.wrapper.events.callevent("player.login", {"player": self.getplayer(username)})

    def logout(self, username):
        """
        Called when a player logs out
        """
        self.wrapper.events.callevent("player.logout", {"player": self.getplayer(username)})
        # if self.wrapper.proxy:
        #     for client in self.wrapper.proxy.clients:
        #         uuid = self.players[username].uuid # This is not used
        if username in self.players:
            self.players[username].abort = True
            del self.players[username]

    def getplayer(self, username):
        """
        Returns a player object with the specified name, or False if the user is not logged in/doesn't exist
        """
        if username in self.players:
            return self.players[username]
        return False

    def reloadproperties(self):
        # Load server icon
        if os.path.exists("%s/server-icon.png" % self.serverpath):
            with open("%s/server-icon.png" % self.serverpath, "rb") as f:
                theicon = f.read()
                iconencoded = base64.standard_b64encode(theicon)
                self.serverIcon = b"data:image/png;base64," + iconencoded
        # Read server.properties and extract some information out of it
        # the PY3.5 ConfigParser seems broken.  This way was much more straightforward and works in both PY2 and PY3
        if os.path.exists("%s/server.properties" % self.serverpath):
            with open("%s/server.properties" % self.serverpath, "r") as f:
                configfile = f.read()
            detect = configfile.split("level-name=")
            if len(detect) < 2:
                self.log.warning("No 'level-name=(worldname)' was found in the server.properties.")
                return False
            self.worldname = detect[1].split("\n")[0]
            self.motd = configfile.split("motd=")[1].split("\n")[0]
            playerentry = configfile.split("max-players=")
            if len(playerentry) < 2:
                self.log.warning("No 'max-players=(count)' was found in the server.properties."
                                 "The default of '20' will be used.")
            else:
                self.maxPlayers = playerentry[1].split("\n")[0]
            self.onlineMode = configfile.split("online-mode=")[1].split("\n")[0]
            if self.onlineMode == "false":
                self.onlineMode = False
            else:
                self.onlineMode = True
            return True
        self.log.warning("File 'server.properties' not found.")
        return False

    def console(self, command):
        """
        Execute a console command on the server
        """
        if self.state in (MCSState.STARTING, MCSState.STARTED, MCSState.STOPPING):
            self.proc.stdin.write("%s\n" % command)
        else:
            raise InvalidServerStateError("Server is not started. Please run '/start' to boot it up.")

    def changestate(self, state, reason=None):
        """
        Change the boot state of the server, with a reason message
        """
        self.state = state
        if self.state == MCSState.OFF:
            self.wrapper.events.callevent("server.stopped", {"reason": reason})
        elif self.state == MCSState.STARTING:
            self.wrapper.events.callevent("server.starting", {"reason": reason})
        elif self.state == MCSState.STARTED:
            self.wrapper.events.callevent("server.started", {"reason": reason})
        elif self.state == MCSState.STOPPING:
            self.wrapper.events.callevent("server.stopping", {"reason": reason})
        self.wrapper.events.callevent("server.state", {"state": state, "reason": reason})

    def getservertype(self):
        if "spigot" in self.config["General"]["command"].lower():
            return "spigot"
        elif "bukkit" in self.config["General"]["command"].lower():
            return "bukkit"
        else:
            return "vanilla"

    def __stdout__(self):
        while not self.wrapper.halt:
            # noinspection PyBroadException,PyUnusedLocal
            try:
                data = self.proc.stdout.readline()
                for line in data.split("\n"):
                    if len(line) < 1:
                        continue
                    self.data.append(line)
            except Exception as e:
                time.sleep(0.1)
                continue

    def __stderr__(self):
        while not self.wrapper.halt:
            try:
                data = self.proc.stderr.readline()
                if len(data) > 0:
                    for line in data.split("\n"):
                        self.data.append(line.replace("\r", ""))
            except Exception as e:
                time.sleep(0.1)
                continue

    def getmemoryusage(self):
        """
        Returns allocated memory in bytes
        This command currently only works for *NIX based systems
        """
        if not resource or not os.name == "posix" or self.proc is None:
            raise UnsupportedOSException("Your current OS (%s) does not support this command at this time." % os.name)
        try:
            with open("/proc/%d/statm" % self.proc.pid, "r") as f:
                getbytes = int(f.read().split(" ")[1]) * resource.getpagesize()
            return getbytes
        except Exception as e:
            raise e

    @staticmethod
    def getstorageavailable(folder):
        """
        Returns the disk space for the working directory in bytes
        """
        if platform.system() == "Windows":
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(folder), None, None,
                                                       ctypes.pointer(free_bytes))
            return free_bytes.value
        else:
            st = os.statvfs(folder)
            return st.f_bavail * st.f_frsize

    @staticmethod
    def stripspecial(text):
        a = ""
        it = iter(range(len(text)))
        for i in it:
            char = text[i]
            if char == "\xc2":
                try:
                    next(it)
                    next(it)
                except Exception as e:
                    pass
            else:
                a += char
        return a

    def readconsole(self, buff):
        """
        Internally-used function that parses a particular console line
        """
        if not self.wrapper.events.callevent("server.consoleMessage", {"message": buff}):
            return False
        if self.getservertype() == "spigot":
            line = " ".join(buff.split(" ")[2:])
        else:
            line = " ".join(buff.split(" ")[3:])
        print(buff)
        deathprefixes = ["fell", "was", "drowned", "blew", "walked", "went", "burned", "hit", "tried",
                         "died", "got", "starved", "suffocated", "withered"]
        if not self.config["General"]["pre-1.7-mode"]:
            if len(getargs(line.split(" "), 0)) < 1:
                return
            if getargs(line.split(" "), 0) == "Done":  # Confirmation that the server finished booting
                self.changestate(MCSState.STARTED)
                self.log.info("Server started")
                self.bootTime = time.time()
            # Getting world name
            elif getargs(line.split(" "), 0) == "Preparing" and getargs(line.split(" "), 1) == "level":
                self.worldname = getargs(line.split(" "), 2).replace('"', "")
                self.world = World(self.worldname, self)
            elif getargs(line.split(" "), 0)[0] == "<":  # Player Message
                name = self.stripspecial(getargs(line.split(" "), 0)[1:-1])
                message = self.stripspecial(getargsafter(line.split(" "), 1))
                original = getargsafter(line.split(" "), 0)
                self.wrapper.events.callevent("player.message", {
                    "player": self.getplayer(name), 
                    "message": message, 
                    "original": original
                })
            elif getargs(line.split(" "), 1) == "logged":  # Player Login
                name = self.stripspecial(getargs(line.split(" "), 0)[0:getargs(line.split(" "), 0).find("[")])
                eid = int(getargs(line.split(" "), 6))
                locationtext = getargs(line.split(" ("), 1)[:-1].split(", ")
                location = int(float(locationtext[0])), int(float(locationtext[1])), int(float(locationtext[2]))
                self.login(name, eid, location)
            elif getargs(line.split(" "), 1) == "left":  # Player Logout
                name = getargs(line.split(" "), 0)
                self.logout(name)
            elif getargs(line.split(" "), 0) == "*":
                name = self.stripspecial(getargs(line.split(" "), 1))
                message = self.stripspecial(getargsafter(line.split(" "), 2))
                self.wrapper.events.callevent("player.action", {
                    "player": self.getplayer(name),
                    "action": message
                })
            elif getargs(line.split(" "), 0)[0] == "[" and getargs(line.split(" "), 0)[-1] == "]":  # /say command
                if self.getservertype != "vanilla":
                    return  # Unfortunately, Spigot and Bukkit output things that conflict with this
                name = self.stripspecial(getargs(line.split(" "), 0)[1:-1])
                message = self.stripspecial(getargsafter(line.split(" "), 1))
                original = getargsafter(line.split(" "), 0)
                self.wrapper.events.callevent("server.say", {
                    "player": name, 
                    "message": message, 
                    "original": original
                })
            # Player Achievement
            elif getargs(line.split(" "), 1) == "has" and getargs(line.split(" "), 5) == "achievement":
                name = self.stripspecial(getargs(line.split(" "), 0))
                achievement = getargsafter(line.split(" "), 6)
                self.wrapper.events.callevent("player.achievement", {
                    "player": name, 
                    "achievement": achievement
                })
            elif getargs(line.split(" "), 1) in deathprefixes:  # Player Death
                name = self.stripspecial(getargs(line.split(" "), 0))
                self.wrapper.events.callevent("player.death", {
                    "player": self.getplayer(name), 
                    "death": getargsafter(line.split(" "), 4)
                })
        else:  # pre 1.7 mode
            if len(getargs(line.split(" "), 3)) < 1:
                return
            if getargs(line.split(" "), 3) == "Done":  # Confirmation that the server finished booting
                self.changestate(MCSState.STARTED)
                self.log.info("Server started")
                self.bootTime = time.time()
            elif getargs(line.split(" "), 3) == "Preparing" and getargs(line.split(" "), 4) == "level":
                # Getting world name
                self.worldname = getargs(line.split(" "), 5).replace('"', "")
                self.world = World(self.worldname, self)
            elif getargs(line.split(" "), 3)[0] == "<":  # Player Message
                name = self.stripspecial(getargs(line.split(" "), 3)[1:-1])
                message = self.stripspecial(getargsafter(line.split(" "), 4))
                original = getargsafter(line.split(" "), 3)
                self.wrapper.events.callevent("player.message", {
                    "player": self.getplayer(name), 
                    "message": message, 
                    "original": original
                })
            elif getargs(line.split(" "), 4) == "logged":  # Player Login
                name = self.stripspecial(getargs(line.split(" "), 3)[0:getargs(line.split(" "), 3).find("[")])
                self.login(name, None, (0, 0, 0))
            elif getargs(line.split(" "), 4) == "lost":  # Player Logout
                name = getargs(line.split(" "), 3)
                self.logout(name)
            elif getargs(line.split(" "), 3) == "*":
                name = self.stripspecial(getargs(line.split(" "), 4))
                message = self.stripspecial(getargsafter(line.split(" "), 5))
                self.wrapper.events.callevent("player.action", {
                    "player": self.getplayer(name), 
                    "action": message
                })
            elif getargs(line.split(" "), 3)[0] == "[" and getargs(line.split(" "), 3)[-1] == "]":  # /say command
                name = self.stripspecial(getargs(line.split(" "), 3)[1:-1])
                message = self.stripspecial(getargsafter(line.split(" "), 4))
                original = getargsafter(line.split(" "), 3)
                if name == "Server":
                    return
                self.wrapper.events.callevent("server.say", {
                    "player": name, 
                    "message": message, 
                    "original": original
                })
            elif getargs(line.split(" "), 4) == "has" and getargs(line.split(" "), 8) == "achievement":
                # Player Achievement
                name = self.stripspecial(getargs(line.split(" "), 3))
                achievement = getargsafter(line.split(" "), 9)
                self.wrapper.events.callevent("player.achievement", {
                    "player": name, 
                    "achievement": achievement
                })
            elif getargs(line.split(" "), 4) in deathprefixes:  # Pre- 1.7 Player Death
                name = self.stripspecial(getargs(line.split(" "), 3))
                # No such config items!
                # deathmessage = self.config["Death"]["death-kick-messages"][random.randrange(
                #     0, len(self.config["Death"]["death-kick-messages"]))]
                # if self.config["Death"]["kick-on-death"] and name in self.config["Death"]["users-to-kick"]:
                #     self.console("kick %s %s" % (name, deathmessage))
                self.wrapper.events.callevent("player.death", {
                    "player": self.getplayer(name), 
                    "death": getargsafter(line.split(" "), 4)
                })

    # mcserver.py onsecond Event Handler
    def eachsecond(self, payload):
        if self.config["General"]["timed-reboot"]:
            if time.time() - self.bootTime > self.config["General"]["timed-reboot-seconds"]:
                if self.config["General"]["timed-reboot-warning-minutes"] > 0:
                    if self.rebootWarnings - 1 < self.config["General"]["timed-reboot-warning-minutes"]:
                        l = (time.time() - self.bootTime - self.config["General"]["timed-reboot-seconds"]) / 60
                        if l > self.rebootWarnings:
                            self.rebootWarnings += 1
                            if int(self.config["General"]["timed-reboot-warning-minutes"] - l + 1) > 0:
                                self.broadcast("&cServer will be rebooting in %d minute(s)!"
                                               % int(self.config["General"]["timed-reboot-warning-minutes"] - l + 1))
                        return
                self.restart("Server is conducting a scheduled reboot. The server will be back momentarily!")
                self.bootTime = time.time()
                self.rebootWarnings = 0
        if self.config["Web"]["web-enabled"]:  # only used by web management module
            if time.time() - self.lastsizepoll > 120:
                if self.worldname is None:
                    return True
                self.lastsizepoll = time.time()
                size = 0
                # os.scandir not in standard library even on early py2.7.x systems
                for i in os.walk("%s/%s" % (self.serverpath, self.worldname)):
                    for f in os.listdir(i[0]):
                        size += os.path.getsize(os.path.join(i[0], f))
                self.worldSize = size


class MCSState:
    """
    This class represents Minecraft Console Server states
    """

    OFF = 0  # this is the start mode.
    STARTING = 1
    STARTED = 2
    STOPPING = 3
    FROZEN = 4

    def __init__(self):
        pass
