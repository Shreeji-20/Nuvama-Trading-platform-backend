import json
import logging
from os import path
import os
import sys
import requests

ModuleLOGGER = logging.getLogger(__name__)

def init_proxies(conf):
    """
    Method to load proxy parameters from configuration file.
    - Parameter:
        conf: ConfigParser object of provided configuration file.
    """

    if conf and 'PROXY' in conf:
        if conf['PROXY'].get('HTTPS_PROXY') and conf['PROXY'].get('HTTP_PROXY'):
            ModuleLOGGER.info("Found proxy related configurations.")
            return {
                    'http': conf['PROXY'].get('HTTP_PROXY'),
                    'https': conf['PROXY'].get('HTTPS_PROXY'),
                }
    return {}

class Http:
    def __init__(self, constants, proxies, ssl_verify=False):

        self.LOGGER = logging.getLogger(__name__)
        self.LOGGER.info("Http object is being created.")

        self.__constants = constants
        self.__requests = requests.session()
        if proxies:
            self.LOGGER.debug("Proxies are setup for HTTP requests.")
            self.__requests.proxies.update(proxies)
            self.__requests.verify = ssl_verify

    def _GetMethod(self, url : str, queryParams : dict = None, sendSource=True):
        if sendSource:
            self.LOGGER.debug("Request to url: %s", url)
            response = self.__requests.get(url, headers={
                "Authorization": self.__constants.JSessionId,
                "Source": self.__constants.ApiKey,
                "SourceToken": self.__constants.VendorSession,
                "AppIdKey": self.__constants.AppIdKey
                },
                params=queryParams)
        else:
            self.LOGGER.debug("Request to url: %s", url)
            response = self.__requests.get(url, headers={
                "Authorization": self.__constants.JSessionId,
                "SourceToken": self.__constants.VendorSession,
                "AppIdKey": self.__constants.AppIdKey
                },
                params=queryParams)
        if response.headers.get('AppIdKey') != "":
            self.__constants.AppIdKey = response.headers.get('AppIdKey')

        if response.status_code == 200:
            return json.loads(response.content)
        elif 200 < response.status_code <= 299:
            self.LOGGER.debug("Response received with status code != 200.")
            self.LOGGER.debug("Error response: %s", response.content.decode('UTF-8'))
            return ""
        else:
            if 'Expired' in response.content.decode('UTF-8'):
                print(json.dumps(json.loads(response.content.decode('UTF-8'))))
                if path.exists(self.__constants.Filename):
                    os.remove(self.__constants.Filename)
                    sys.exit()
                print("Expired session.")
            else:
                return json.loads(response.content)
            print(response.content.decode('UTF-8'))
            self.LOGGER.debug("Error response: %s", response.content.decode('UTF-8'))
            return ""

    def _PostMethod(self, url : str, data : str, sendSource=True):
        if sendSource:
            response = self.__requests.post(url, headers={
                "Authorization": self.__constants.JSessionId,
                "Source": self.__constants.ApiKey,
                "SourceToken": self.__constants.VendorSession,
                "AppIdKey": self.__constants.AppIdKey,
                "Content-type": "application/json"}, data=data)

        else:
            response = self.__requests.post(url, headers={
                "Authorization": self.__constants.JSessionId,
                "SourceToken": self.__constants.VendorSession,
                "AppIdKey": self.__constants.AppIdKey,
                "Content-type": "application/json"}, data=data)

        if response.headers.get('AppIdKey') != "":
            self.__constants.AppIdKey = response.headers.get('AppIdKey')
        if response.status_code == 200:
            return json.loads(response.content)
        else:
            if 'Expired' in response.content.decode('UTF-8'):
                print(json.dumps(json.loads(response.content.decode('UTF-8'))))
                if path.exists(self.__constants.Filename):
                    os.remove(self.__constants.Filename)
                print("Expired session.")
            else:
                return json.loads(response.content)
            self.LOGGER.debug("Error response: %s", response.content.decode('UTF-8'))
            return ""

    def _PutMethod(self, url : str, data : str):
        response = self.__requests.put(url, headers={"Authorization": self.__constants.JSessionId,
                                                "Source": self.__constants.ApiKey,
                                                "SourceToken": self.__constants.VendorSession,
                                                "AppIdKey": self.__constants.AppIdKey,
                                                "Content-type": "application/json"}, data=data)
        if response.headers.get('AppIdKey') != "":
            self.__constants.AppIdKey = response.headers.get('AppIdKey')
        if response.status_code == 200:
            return json.loads(response.content)

        else:
            if 'Expired' in response.content.decode('UTF-8'):
                if path.exists(self.__constants.Filename):
                    os.remove(self.__constants.Filename)
                print("Expired session.")
            else:
                return json.loads(response.content)
            self.LOGGER.debug("Error response: %s", response.content.decode('UTF-8'))
            return ""

    def _DeleteMethod(self, url : str, data : str):
        response = self.__requests.delete(url, headers={"Authorization": self.__constants.JSessionId,
                                                    "Source": self.__constants.ApiKey,
                                                    "SourceToken": self.__constants.VendorSession,
                                                    "AppIdKey": self.__constants.AppIdKey,
                                                    "Content-type": "application/json"}, data=data)
        if response.headers.get('AppIdKey') != "":
            self.__constants.AppIdKey = response.headers.get('AppIdKey')
        if response.status_code == 200:
            return json.loads(response.content)

        else:
            if 'Expired' in response.content.decode('UTF-8'):
                if path.exists(self.__constants.Filename):
                    os.remove(self.__constants.Filename)
                print("Expired session.")
            else:
                return json.loads(response.content)
            self.LOGGER.debug("Error response: %s", response.content.decode('UTF-8'))
            return ""


