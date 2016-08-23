# Copyright (c) 2016 Electric Imp
# This file is licensed under the MIT License
# http://opensource.org/licenses/MIT

import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib

import sublime
import sublime_plugin

# Plugin specific imports
from plugin_resources.strings import *

# Append requests module to the system module path
sys.path.append(os.path.join(os.path.dirname(__file__), "requests"))
import requests

# Generic plugin constants
PL_BUILD_API_URL         = "https://build.electricimp.com/v4/"
PL_SETTINGS_FILE         = "ImpDeveloper.sublime-settings"
PL_DEBUG_FLAG            = "debug"
PL_AGENT_URL             = "https://agent.electricimp.com/{}"
PL_WIN_PROGRAMS_DIR_32   = "C:\\Program Files (x86)\\"
PL_WIN_PROGRAMS_DIR_64   = "C:\\Program Files\\"
PL_LOG_START_TIME        = "2000-01-01T00:00:00.000+00:00"
PR_LOGS_UPDATE_PERIOD    = 5000 # ms

# Electric Imp project specific constants
PR_DEFAULT_PROJECT_NAME  = "electric-imp-project"
PR_TEMPLATE_DIR_NAME     = "project-template"
PR_PROJECT_FILE_TEMPLATE = "_project_name_.sublime-project"
PR_SETTINGS_FILE         = "electric-imp.settings"
PR_BUILD_API_KEY_FILE    = "build-api.key"
PR_SOURCE_DIRECTORY      = "src"
PR_SETTINGS_DIRECTORY    = "settings"
PR_BUILD_DIRECTORY       = "build"
PR_DEVICE_FILE_NAME      = "device.nut"
PR_AGENT_FILE_NAME       = "agent.nut"
PR_PREPROCESSED_PREFIX   = "preprocessed."

# Electric Imp settings and project properties
EI_BUILD_API_KEY         = "build-api-key"
EI_MODEL_ID              = "model-id"
EI_DEVICE_FILE           = "device-file"
EI_AGENT_FILE            = "agent-file"
EI_DEVICE_ID             = "device-id"

# Global variables
plugin_settings = None
project_windows = []


class ProjectManager:
    """Electric Imp project specific fuctionality"""

    def __init__(self, window):
        self.window = window

    def get_settings_dir(self):
        project_file_name = self.window.project_file_name()
        if project_file_name:
            project_dir = os.path.dirname(project_file_name)
            return os.path.join(project_dir, PR_SETTINGS_DIRECTORY)

    @staticmethod
    def dump_map_to_json_file(filename, map):
        with open(filename, "w") as file:
            json.dump(map, file)

    def save_settings(self, filename, settings):
        self.dump_map_to_json_file(self.get_settings_file_path(filename), settings)

    def load_settings(self, filename):
        path = self.get_settings_file_path(filename)
        if path and os.path.exists(path):
            with open(path) as file:
                return json.load(file)

    def get_settings_file_path(self, filename):
        settings_dir = self.get_settings_dir()
        if settings_dir and filename:
            return os.path.join(settings_dir, filename)

    def is_electric_imp_project(self):
        settings_filename = self.get_settings_file_path(PR_SETTINGS_FILE)
        return settings_filename is not None and os.path.exists(settings_filename)

    def get_build_api_key(self):
        api_key_map = self.load_settings(PR_BUILD_API_KEY_FILE)
        if api_key_map:
            return api_key_map.get(EI_BUILD_API_KEY)


class Env:
    """Window (project) specific environment object"""

    def __init__(self, window):
        # UI Manager
        self.ui_manager = UIManager(window)
        # Electric Imp Project manager
        self.project_manager = ProjectManager(window)
        # Preprocessor
        self.preprocessor = Preprocessor(window, self.project_manager.load_settings(PR_SETTINGS_FILE))

    @staticmethod
    def For(window):
        return window.__ei_env__

    @staticmethod
    def create_env_if_doesnt_exist_for(window):
        if window.__ei_env__ is None:
            window.__ei_env__ = Env(window)
        # There is nothing to do if the window has an environment registered already
        return window.__ei_env__

    @staticmethod
    def unregister_env_for(window):
        window.__ei_env__ = None


class UIManager:
    """Electric Imp plugin UI manager"""

    def __init__(self, window):
        self.window = window

    def create_new_console(self):
        env = Env.For(self.window)
        env.terminal = self.window.get_output_panel("textarea")
        env.logs_timestamp = PL_LOG_START_TIME

    def write_to_console(self, text):
        terminal = Env.For(self.window).terminal
        terminal.set_read_only(False)
        terminal.run_command("append", {"characters": text + "\n"})
        terminal.set_read_only(True)

    def init_tty(self):
        global project_windows
        if self.window not in project_windows:
            self.create_new_console()
            project_windows.append(self.window)
            log_debug(
                "adding new project window: " + str(self.window) + ", total windows now: " + str(len(project_windows)))
        self.show_console()

    def show_console(self):
        self.window.run_command("show_panel", {"panel": "output.textarea"})


class HTTPConnection:
    """Implementation of all the Electric Imp connection functionality"""

    @staticmethod
    def __base64_encode(str):
        return base64.b64encode(str.encode()).decode()

    @staticmethod
    def __get_http_headers(key):
        return {
            "Authorization": "Basic " + HTTPConnection.__base64_encode(key),
            "Content-Type": "application/json"
        }

    @staticmethod
    def is_build_api_key_valid(key):
        return requests.get(PL_BUILD_API_URL + "models",
                            headers=HTTPConnection.__get_http_headers(key)).status_code == requests.codes.ok

    @staticmethod
    def get(key, url):
        return requests.get(url, headers=HTTPConnection.__get_http_headers(key))

    @staticmethod
    def post(key, url, data=None):
        return requests.post(url, data=data, headers=HTTPConnection.__get_http_headers(key))

    @staticmethod
    def is_response_valid(response):
        return response.status_code != requests.codes.ok


class Preprocessor:
    """Preprocessor and Builder specific implementation"""

    class SourceType():
        AGENT  = 0
        DEVICE = 1

    def __init__(self, window, settings):
        self.window = window
        self.settings = settings
        self.line_table = {Preprocessor.SourceType.AGENT: None, Preprocessor.SourceType.DEVICE: None}

    @staticmethod
    def get_root_nodejs_dir_path():
        result = None
        platform = sublime.platform()
        if platform == "windows":
            path64 = PL_WIN_PROGRAMS_DIR_64 + "nodejs\\"
            path32 = PL_WIN_PROGRAMS_DIR_32 + "nodejs\\"
            if os.path.exists(path64):
                result = path64
            elif os.path.exists(path32):
                result = path32
        elif platform in ["linux", "osx"]:
            bin_dir = "bin/node"
            js_dir1 = "/usr/local/nodejs/"
            js_dir2 = "/usr/local/"
            if os.path.exists(os.path.join(js_dir1, bin_dir)):
                result = js_dir1
            elif os.path.exists(os.path.join(js_dir2, bin_dir)):
                result = js_dir2
        return result

    def get_node_path(self):
        platform = sublime.platform()
        if platform == "windows":
            return self.get_root_nodejs_dir_path() + "bin\\node"
        elif platform in ["linux", "osx"]:
            return self.get_root_nodejs_dir_path() + "bin/node"

    def get_node_cli_path(self):
        platform = sublime.platform()
        if platform == "windows":
            return self.get_root_nodejs_dir_path() + "lib\\node_modules\\Builder\\src\\cli.js"
        elif platform in ["linux", "osx"]:
            return self.get_root_nodejs_dir_path() + "lib/node_modules/Builder/src/cli.js"

    def preprocess(self):
        src_dir = os.path.join(os.path.dirname(self.window.project_file_name()), PR_SOURCE_DIRECTORY)
        bld_dir = os.path.join(os.path.dirname(self.window.project_file_name()), PR_BUILD_DIRECTORY)

        source_agent_filename = os.path.join(src_dir, self.settings.get(EI_AGENT_FILE))
        result_agent_filename = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_AGENT_FILE_NAME)
        source_device_filename = os.path.join(src_dir, self.settings.get(EI_DEVICE_FILE))
        result_device_filename = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_DEVICE_FILE_NAME)

        if not os.path.exists(bld_dir):
            os.makedirs(bld_dir)

        for code_files in [[
            source_agent_filename,
            result_agent_filename
        ], [
            source_device_filename,
            result_device_filename
        ]]:
            try:
                args = [
                    self.get_node_path(),
                    self.get_node_cli_path(),
                    "-l",
                    code_files[0]
                ]
                with open(code_files[1], "w") as output:
                    subprocess.check_call(args, stdout=output)
            except subprocess.CalledProcessError as error:
                log_debug("Error running preprocessor. The process returned code: " + error.returncode)

        for source_type in self.line_table:
            self.line_table[source_type] = self.__build_line_table(source_type)

        return result_agent_filename, result_device_filename

    def __build_line_table(self, source_type):
        # Setup the preprocessed file name based on the source type
        bld_dir = os.path.join(os.path.dirname(self.window.project_file_name()), PR_BUILD_DIRECTORY)
        if source_type == self.SourceType.AGENT:
            preprocessed_file_path = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_AGENT_FILE_NAME)
            orig_filename = PR_AGENT_FILE_NAME
        elif source_type == self.SourceType.DEVICE:
            preprocessed_file_path = os.path.join(bld_dir, PR_PREPROCESSED_PREFIX + PR_DEVICE_FILE_NAME)
            orig_filename = PR_DEVICE_FILE_NAME
        else:
            log_debug("Wrong source type")
            return

        # Parse the target file and build the code line table
        line_table = {}
        pattern = re.compile(r"#line \d+ \"*\"")
        curr_line = orig_line = 0
        with open(preprocessed_file_path, 'r', encoding="utf-8") as f:
            while 1:
                line = f.readline()
                if not line:
                    break
                match = pattern.search(line)
                if match:
                    orig_line, orig_filename = match.group()
                line_table[curr_line] = (orig_filename, int(orig_line))
                orig_line += 1
                curr_line += 1

        return line_table

    # Converts error location in the preprocessed code into the original filename and line number
    def get_error_location(self, source_type, line):
        code_table = self.line_table[source_type]
        return None if code_table is None else code_table[line]


class BaseElectricImpCommand(sublime_plugin.WindowCommand):
    """The base class for all the Electric Imp Commands"""

    def __init__(self, window):
        self.window = window
        self.__tmp_device_ids = None

        self.env = Env.create_env_if_doesnt_exist_for(window)

    def prompt_for_settings_if_missing(self):
        # Check if Build API key exists
        if not self.env.project_manager.get_build_api_key():
            decision = sublime.ok_cancel_dialog(STR_MISSING_API_KEY)
            if decision:
                self.prompt_for_build_api_key()
            return

        # Prompt for device if it wasn't selected yet
        if EI_DEVICE_ID not in self.env.project_manager.load_settings(PR_SETTINGS_FILE):
            decision = sublime.ok_cancel_dialog(STR_SELECT_DEVICE)
            if decision:
                self.prompt_for_device()

    def print_to_tty(self, text):
        global project_windows
        if self.window in project_windows:
            self.env.ui_manager.write_to_console(text)
        else:
            print(text)

    def prompt_for_device(self):
        model_id = self.env.project_manager.load_settings(PR_SETTINGS_FILE).get(EI_MODEL_ID)
        url = PL_BUILD_API_URL + "models/" + model_id
        response = HTTPConnection.get(self.env.project_manager.get_build_api_key(), url)
        # If request failed, the model doesn't seem to exist anymore
        if not HTTPConnection.is_response_valid(response):
            sublime.message_dialog(STR_MODEL_DOES_NOT_EXIST.format(model_id))
            return
        response_json = response.json()
        self.__tmp_device_ids = response_json.get("model").get("devices")
        if len(self.__tmp_device_ids) > 0:
            self.window.show_quick_panel(self.__tmp_device_ids, self.on_device_selected)
        else:
            sublime.message_dialog(STR_NO_DEVICES_AVAILABLE)

    def prompt_for_build_api_key(self):
        sublime.message_dialog(STR_ENTER_BUILD_API_KEY)
        self.window.show_input_panel(STR_BUILD_API_KEY, "", self.on_build_api_key_entered, None, None)

    def on_build_api_key_entered(self, key):
        log_debug("build api key provided: " + key)
        if self.is_build_api_key_valid(key):
            log_debug("build API key is valid")
            self.save_settings(PR_BUILD_API_KEY_FILE, {
                EI_BUILD_API_KEY: key
            })
        else:
            if sublime.ok_cancel_dialog(STR_INVALID_API_KEY):
                self.prompt_for_build_api_key()
        self.prompt_for_settings_if_missing()

    def on_device_selected(self, index):
        settings = self.env.project_manager.load_settings(PR_SETTINGS_FILE)
        new_device_id = self.__tmp_device_ids[index]
        old_device_id = None if EI_DEVICE_ID not in settings else settings.get(EI_DEVICE_ID)
        if new_device_id != old_device_id:
            log_debug("New device selected: saving new settings file and restarting the console...")
            # Update the device id
            settings[EI_DEVICE_ID] = self.__tmp_device_ids[index]
            self.env.project_manager.save_settings(PR_SETTINGS_FILE, settings)
            # Clean up the terminal window
            self.env.ui_manager.create_new_console()
            self.env.ui_manager.show_console()
        else:
            log_debug("Newly selected device is the same as the old one. Nothing to do.")
        # Clean up temporary variables
        self.__tmp_device_ids = None

    def is_enabled(self):
        return self.env.project_manager.is_electric_imp_project()


class ImpPushCommand(BaseElectricImpCommand):
    """Code push command implementation"""

    def __init__(self, window):
        super(ImpPushCommand, self).__init__(window)

    def run(self):
        self.env.ui_manager.init_tty()
        self.prompt_for_settings_if_missing()

        if self.env.project_manager.get_build_api_key() is None:
            log_debug("The build API file is missing, please check the settings")
            return

        # Save all the views first
        self.save_all_current_window_views()

        # Preprocess the sources
        agent_filename, device_filename = self.preprocessor.preprocess()

        if not os.path.exists(agent_filename) or not os.path.exists(device_filename):
            log_debug("Can't find code files")
            sublime.message_dialog(STR_CODE_IS_ABSENT.format(self.get_settings_file_path(PR_SETTINGS_FILE)))

        agent_code  = self.read_file(agent_filename)
        device_code = self.read_file(device_filename)

        settings = self.env.project_manager.load_settings(PR_SETTINGS_FILE)
        url = PL_BUILD_API_URL + "models/" + settings.get(EI_MODEL_ID) + "/revisions"
        data = '{"agent_code": ' + json.dumps(agent_code) + ', "device_code" : ' + json.dumps(device_code) + ' }'
        response = HTTPConnection.post(self.env.project_manager.get_build_api_key(), url, data)

        # Update the logs first
        update_log_windows(False)

        # Process response and handle errors appropriately
        self.process_response(response, settings)

    def process_response(self, response, settings):
        if HTTPConnection.is_response_valid(response):
            response_json = response.json()
            self.print_to_tty("Revision uploaded: " + str(response_json["revision"]["version"]))

            # Not it's time to restart the Model
            url = PL_BUILD_API_URL + "models/" + settings.get(EI_MODEL_ID) + "/restart"
            HTTPConnection.post(self.env.project_manager.get_build_api_key(), url)
        else:
            # {
            # 	'error': {
            # 		'message_short': 'Device code compile failed',
            # 		'details': {
            # 			'agent_errors': None,
            # 			'device_errors': [
            # 				{
            # 					'row': 3,
            # 					'error': 'expression expected',
            # 					'column': 24
            # 				}
            # 			]
            # 		},
            # 		'code': 'CompileFailed',
            # 		'message_full': 'Device code compile failed\nDevice Errors:\n3:24 expression expected'
            # 	},
            # 	'success': False
            # }
            def build_error_list(errors, errorLocation):
                report = ""
                if errors is not None:
                    report = "		{} code:\n".format(errorLocation)
                    for e in errors:
                        report += "				Line: {}, Column: {}, Message: {}\n".format(e["row"], e["column"],
                                                                                               e["error"])
                return report

            response_json = response.json()
            error = response_json["error"]
            if error and error["code"] == "CompileFailed":
                error_message = "Deploy failed because of the compilation errors:\n"
                error_message += build_error_list(error["details"]["agent_errors"], "Agent")
                error_message += build_error_list(error["details"]["device_errors"], "Device")
                self.print_to_tty(error_message)
            else:
                log_debug("Code deply failed because of unknown error: {}".format(str(response_json["error"]["code"])))

    def save_all_current_window_views(self):
        log_debug("Saving all views...")
        self.window.run_command("save_all")

    @staticmethod
    def read_file(filename):
        with open(filename, 'r', encoding="utf-8") as f:
            s = f.read()
        return s


class ImpShowConsoleCommand(BaseElectricImpCommand):
    def run(self):
        self.env.ui_manager.init_tty()
        self.prompt_for_settings_if_missing()


class ImpSelectDeviceCommand(BaseElectricImpCommand):
    def run(self):
        self.prompt_for_device()


class ImpGetAgentUrlCommand(BaseElectricImpCommand):
    def run(self):
        self.prompt_for_settings_if_missing()
        settings = self.load_settings(PR_SETTINGS_FILE)
        if EI_DEVICE_ID in settings:
            device_id = settings.get(EI_DEVICE_ID)
            response  = HTTPConnection.get(self.env.project_manager.get_build_api_key(),
                                      PL_BUILD_API_URL + "devices/" + device_id).json()
            agent_id  = response.get("device").get("agent_id")
            agent_url = PL_AGENT_URL.format(agent_id)
            sublime.set_clipboard(agent_url)
            sublime.message_dialog(STR_AGENT_URL_COPIED.format(device_id, agent_url))


class ImpCreateProjectCommand(BaseElectricImpCommand):

    def __init__(self, window):
        super(ImpCreateProjectCommand, self).__init__(window)

        # Define all the temporary variables
        self.__tmp_model_id = None
        self.__tmp_project_path = None
        self.__tmp_all_model_ids = None
        self.__tmp_build_api_key = None
        self.__tmp_all_model_names = None

    def run(self):
        self.window.show_input_panel(STR_NEW_PROJECT_LOCATION,
                                     self.get_default_project_path(),
                                     self.on_project_path_entered,
                                     None,
                                     None)

    @staticmethod
    def get_default_project_path():
        global plugin_settings
        default_project_path_setting = plugin_settings.get(PR_DEFAULT_PROJECT_NAME)
        if not default_project_path_setting:
            if sublime.platform() == "windows":
                default_project_path = os.path.expanduser("~\\" + PR_DEFAULT_PROJECT_NAME).replace("\\", "/")
            else:
                default_project_path = os.path.expanduser("~/" + PR_DEFAULT_PROJECT_NAME)
        else:
            default_project_path = default_project_path_setting
        return default_project_path

    def on_project_path_entered(self, path):
        log_debug("Project path specified: " + path)
        self.__tmp_project_path = path

        if os.path.exists(path):
            if not sublime.ok_cancel_dialog(STR_FOLDER_EXISTS.format(path)):
                return
        self.prompt_for_build_api_key()

    def on_build_api_key_entered(self, key):
        log_debug("build api key provided: " + key)
        if self.is_build_api_key_valid(key):
            log_debug("build API key is valid")
            self.__tmp_build_api_key = key
            self.prompt_for_model()
        else:
            if sublime.ok_cancel_dialog(STR_INVALID_API_KEY):
                self.prompt_for_build_api_key()

    def prompt_for_model(self):
        response = HTTPConnection.get(self.__tmp_build_api_key, PL_BUILD_API_URL + "models").json()
        if len(response["models"]) > 0:
            if not sublime.ok_cancel_dialog(STR_SELECT_MODEL):
                return
            self.__tmp_all_model_names = [model["name"] for model in response["models"]]
            self.__tmp_all_model_ids = [model["id"] for model in response["models"]]
        else:
            sublime.message_dialog(STR_NO_MODELS_AVAILABLE)
            return

        self.window.show_quick_panel(self.__tmp_all_model_names, self.on_model_choosen)

    def on_model_choosen(self, index):
        self.__tmp_model_id = self.__tmp_all_model_ids[index]
        log_debug("model chosen (name, id): (" + self.__tmp_all_model_names[index] + ", " + self.__tmp_model_id + ")")
        self.create_project()

    def create_project(self):
        source_dir = os.path.join(self.__tmp_project_path, PR_SOURCE_DIRECTORY)
        settings_dir = os.path.join(self.__tmp_project_path, PR_SETTINGS_DIRECTORY)

        log_debug("Creating project at: " + self.__tmp_project_path)
        if not os.path.exists(source_dir):
            os.makedirs(source_dir)
        if not os.path.exists(settings_dir):
            os.makedirs(settings_dir)

        self.copy_project_template_file()
        self.copy_gitignore()

        # Create Electric Imp project settings file
        self.env.project_manager.dump_map_to_json_file(os.path.join(settings_dir, PR_SETTINGS_FILE), {
            EI_MODEL_ID:    self.__tmp_model_id,
            EI_AGENT_FILE:  PR_AGENT_FILE_NAME,
            EI_DEVICE_FILE: PR_DEVICE_FILE_NAME
        })

        # Create Electric Imp project settings file
        self.env.project_manager.dump_map_to_json_file(os.path.join(settings_dir, PR_BUILD_API_KEY_FILE), {
            EI_BUILD_API_KEY: self.__tmp_build_api_key
        })

        # Pull the latest code revision from the server
        self.pull_model_revision()

        try:
            # Try opening the project in the new window
            self.run_sublime_from_command_line(["-n", self.get_project_file_name()])
        except:
            log_debug("Error executing sublime: {} ".format(sys.exc_info()[0]))
            # If failed, open the project in the file browser
            self.window.run_command("open_dir", {"dir": self.__tmp_project_path})

        # Clean up all temporary variables
        self.__tmp_model_id = None
        self.__tmp_project_path = None
        self.__tmp_all_model_ids = None
        self.__tmp_build_api_key = None
        self.__tmp_all_model_names = None

    @staticmethod
    def get_sublime_path():
        platform = sublime.platform()
        if platform == "osx":
            return "/Applications/Sublime Text.app/Contents/SharedSupport/bin/subl"
        elif platform == "windows":
            path64 = PL_WIN_PROGRAMS_DIR_64 + "Sublime Text 3\\sublime_text.exe"
            path32 = PL_WIN_PROGRAMS_DIR_32 + "Sublime Text 3\\sublime_text.exe"
            if os.path.exists(path64):
                return path64
            elif os.path.exists(path32):
                return path32
        elif platform == "linux":
            return "/opt/sublime/sublime_text"
        else:
            log_debug("Unknown platform: {}".format(platform))

    def run_sublime_from_command_line(self, args):
        args.insert(0, self.get_sublime_path())
        return subprocess.Popen(args)

    def get_project_file_name(self):
        return os.path.join(self.__tmp_project_path,
                            PR_PROJECT_FILE_TEMPLATE.replace("_project_name_",
                                                             os.path.basename(self.__tmp_project_path)))

    def copy_project_template_file(self):
        src = os.path.join(self.get_template_dir(), PR_PROJECT_FILE_TEMPLATE)
        dst = self.get_project_file_name()
        shutil.copy(src, dst)

    def copy_gitignore(self):
        src = os.path.join(self.get_template_dir(), ".gitignore")
        shutil.copy(src, self.__tmp_project_path)

    def pull_model_revision(self):
        source_dir  = os.path.join(self.__tmp_project_path, PR_SOURCE_DIRECTORY)
        agent_file  = os.path.join(source_dir, PR_AGENT_FILE_NAME)
        device_file = os.path.join(source_dir, PR_DEVICE_FILE_NAME)

        revisions = HTTPConnection.get(self.__tmp_build_api_key,
                                  PL_BUILD_API_URL + "models/" + self.__tmp_model_id + "/revisions").json()
        if len(revisions["revisions"]) > 0:
            latest_revision_url = PL_BUILD_API_URL + "models/" + self.__tmp_model_id + "/revisions/" + \
                                  str(revisions["revisions"][0]["version"])
            code = HTTPConnection.get(self.__tmp_build_api_key, latest_revision_url).json()
            with open(agent_file, "w", encoding="utf-8") as file:
                file.write(code["revision"]["agent_code"])
            with open(device_file, "w", encoding="utf-8") as file:
                file.write(code["revision"]["device_code"])
        else:
            # Create empty files
            open(agent_file, 'a').close()
            open(device_file, 'a').close()

    def get_template_dir(self):
        return os.path.join(os.path.dirname(os.path.realpath(__file__)), PR_TEMPLATE_DIR_NAME)

    # Create Project menu item is always enabled regardless of the project type
    def is_enabled(self):
        return True


def log_debug(text):
    global plugin_settings
    if plugin_settings.get(PL_DEBUG_FLAG):
        print("  [EI::Debug] " + text)


def plugin_loaded():
    global plugin_settings
    plugin_settings = sublime.load_settings(PL_SETTINGS_FILE)


def update_log_windows(restart_timer=True):
    global project_windows
    try:
        for window in project_windows:
            env = Env.For(window)
            eiCommand = BaseElectricImpCommand(window)
            if not eiCommand.project_manager.is_electric_imp_project():
                # It's not a windows that corresponds to an EI project, remove it from the list
                project_windows.remove(window)
                env.unregister_env_for(window)
                log_debug("Removing project window: " + str(window) + ", total #: " + str(len(project_windows)))
                continue
            device_id = eiCommand.project_manager.load_settings(PR_SETTINGS_FILE).get(EI_DEVICE_ID)
            timestamp = env.logs_timestamp
            if None in [device_id, timestamp, eiCommand.project_manager.get_build_api_key()]:
                # Device is not selected yet and the console is not setup for the project, nothing we can do here
                continue
            url = PL_BUILD_API_URL + "devices/" + device_id + "/logs?since=" + urllib.parse.quote(timestamp)
            response = HTTPConnection.get(eiCommand.project_manager.get_build_api_key(), url)

            # There was an error while retrieving logs from the server
            if not HTTPConnection.is_response_valid(response):
                log_debug(STR_FAILED_TO_GET_LOGS)
                continue

            response_json = response.json()
            log_size = 0 if "logs" not in response_json else len(response_json["logs"])
            if log_size > 0:
                timestamp = response_json["logs"][log_size - 1]["timestamp"]
                env.logs_timestamp = timestamp
                for log in response_json["logs"]:
                    type = {
                        "status"       : "[Server]",
                        "server.log"   : "[Device]",
                        "server.error" : "[Device]",
                        "lastexitcode" : "[Device]",
                        "agent.log"    : "[Agent] ",
                        "agent.error"  : "[Agent] "
                    }[log["type"]]
                    try:
                        pass
                    except:
                        log_debug("Unrecognized log type: " + log["type"])
                        type = "[Unrecognized]"
                    dt = datetime.datetime.strptime("".join(log["timestamp"].rsplit(":", 1)), "%Y-%m-%dT%H:%M:%S.%f%z")
                    eiCommand.print_to_tty(dt.strftime('%Y-%m-%d %H:%M:%S%z') + " " + type + " " + log["message"])
    finally:
        if restart_timer:
            sublime.set_timeout_async(update_log_windows, PR_LOGS_UPDATE_PERIOD)

update_log_windows()