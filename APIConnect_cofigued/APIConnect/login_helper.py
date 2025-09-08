import csv
import json
import logging
import sys
import urllib
import zipfile
from APIConnect.api_constants import ApiConstants
import redis
from APIConnect.http import Http
from constants.router import Router
import traceback
import pandas as pd
class LoginHelper:

    def __init__(self, http_obj : Http, router_obj: Router, constants_obj: ApiConstants, proxies : dict) -> None:

        self.LOGGER = logging.getLogger(__name__)
        self.LOGGER.info("LoginHelper object is being created.")
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        self.__http : Http = http_obj
        self.__router : Router = router_obj
        self.__constants : ApiConstants = constants_obj
        self.__proxies : dict = proxies


    def LOGGER_print(self):
        self.LOGGER.error("printing this error")


    def _GenerateVendorSession(self, ApiKey, Password):
        """

        Get Login Info.

        ApiKey : Key provided by Nuvama

        Password : Password provided by Nuvama

        """
        self.__Login(ApiKey, Password)
        self.LOGGER.info("Vendor session generated.")


    def _GetAuthorization(self, reqId):
        """

        Get Login Info.

        reqId : Request ID generated during re-direction to a url

        """
        self.__Token(reqId)
        self.LOGGER.info("Authorization done succesfully.")

    def __Login(self, source, password):
        params = locals()
        del (params["self"])
        url = self.__router._LoginURL().format(vendorId=source)
        rep = self.__http._PostMethod(url, json.dumps({"pwd": password}))
        if rep != "" and 'msg' in rep:
            vt = rep['msg']
            self.__constants.VendorSession = vt
            self.LOGGER.info("User logged in successfully.")
        else:
            raise Exception("Login failed. Reason",rep)
            # self.LOGGER.info("User unable to login.")
            # self.LOGGER.debug("User unable to login.")
            # sys.exit()

    def __Token(self, reqId):
        params = locals()
        del (params["self"])
        url = self.__router._TokenURL()
        rep = self.__http._PostMethod(url, json.dumps({"reqId": reqId}), False)
        
        if rep != "" and 'data' in rep:
            self.__constants.Data = rep
            if rep['data']['lgnData']['accTyp'] == 'EQ':
                self.__constants.eqAccId = rep['data']['lgnData']['accs']['eqAccID']
            elif rep['data']['lgnData']['accTyp'] == 'CO':
                self.__constants.coAccId = rep['data']['lgnData']['accs']['coAccID']
            elif rep['data']['lgnData']['accTyp'] == 'COMEQ':
                self.__constants.eqAccId = rep['data']['lgnData']['accs']['eqAccID']
                self.__constants.coAccId = rep['data']['lgnData']['accs']['coAccID']

            self.__constants.JSessionId = rep['data']['auth']

            prop = json.dumps({'vt': self.__constants.VendorSession,
                               'auth': self.__constants.JSessionId,
                               'eqaccid': self.__constants.eqAccId,
                               'coaccid': self.__constants.coAccId,
                               'data': self.__constants.Data,
                               'appidkey': self.__constants.AppIdKey})
            self.r.set(self.__constants.Filename, prop)
            self.r.expire(self.__constants.Filename, 43200)  # Set expiration to 12 hours
            # self.LOGGER.debug("Login details are pick
            # writetofile = open(self.__constants.Filename, 'w').write(prop)
            self.LOGGER.debug("Login details are pickled in file.")
        else:
            print(rep)
            
            raise Exception(f"Login failed Reason -> + {rep['error']['errMsg']}")
            # print("\nYour login Request ID has expired, kindly regenerate it and try again!")
            # self.LOGGER.debug("Login request id has expired. Need to regenerate.")
            # sys.exit()

    def _Instruments(self, downloadContract, proxies):
        __instruments = []
        # __mfInstruments = []
        try:
            if downloadContract:
                url = self.__router.EquityContractURL
                if proxies:
                    proxy = urllib.request.ProxyHandler(self.__proxies)
                    opener = urllib.request.build_opener(proxy)
                    urllib.request.install_opener(opener)
                    import ssl
                    ssl._create_default_https_context = ssl._create_unverified_context
                
                urllib.request.urlretrieve(url, 'instruments.zip')
                self.LOGGER.info("Downloaded instruments.zip")
                # url = self.__router.MFContractURL
                # urllib.request.urlretrieve(url, 'mfInstruments.zip')
                # self.LOGGER.info("Downloaded mfInstruments.zip")

            with zipfile.ZipFile('instruments.zip', 'r') as zip_ref:
                zip_ref.extractall('instruments')
                self.LOGGER.info("Extracted instruments.csv")
            with open('instruments/instruments.csv', mode='r') as csv_file:
                csv_reader = csv.DictReader(csv_file)
                for row in csv_reader:
                    __instruments.append(row)
                self.LOGGER.info("Loaded instruments.csv")
                
            if not self.r.exists("instruments_csv"):
                self.r.set("instruments_csv", json.dumps(__instruments),ex=43200)
                df = pd.DataFrame(__instruments)
                nifty_lot_size = int(df[df['symbolname'] == "NIFTY"].iloc[0]['lotsize'])
                banknifty_lot_size = int(df[df['symbolname'] == "BANKNIFTY"].iloc[0]['lotsize'])
                sensex_lot_size = int(df[df['symbolname'] == "SENSEX"].iloc[0]['lotsize'])
                finnifty_lot_size = int(df[df['symbolname'] == "FINNIFTY"].iloc[0]['lotsize'])
                
                lot_size_dict = {
                    "NIFTY":nifty_lot_size,
                    "BANKNIFTY":banknifty_lot_size,
                    "FINNIFTY":finnifty_lot_size,
                    "SENSEX":sensex_lot_size
                }
                self.r.set("lotsizes",json.dumps(lot_size_dict))
            # with zipfile.ZipFile('mfInstruments.zip', 'r') as zip_ref:
            #     zip_ref.extractall('mfInstruments')
            #     self.LOGGER.info("Extracted mfInstruments.csv")
            # with open('mfInstruments/mfInstruments.csv', mode='r') as csv_file:
            #     csv_reader = csv.DictReader(csv_file)
            #     for row in csv_reader:
            #         __mfInstruments.append(row)
            #     self.LOGGER.info("Loaded mfInstruments.csv")

        except Exception as ex:
            self.LOGGER.exception("Error occurred while downloading/ reading instruments: %s", ex)
            print("Error Download/Reading Instruments",traceback.format_exc())