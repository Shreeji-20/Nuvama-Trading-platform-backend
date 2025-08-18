import json
import logging
import sys

from APIConnect.api_constants import ApiConstants
from APIConnect.http import Http
from constants.action import ActionEnum
from constants.router import Router
import redis

class ApiUtils:

    def __init__(self, version : str, http_obj : Http, router_obj : Router, constants_obj : ApiConstants) -> None:
        self.LOGGER = logging.getLogger(__name__)
        self.__version : str = version
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        self.__http : Http = http_obj
        self.__router : Router = router_obj
        self.__constants : ApiConstants= constants_obj
        self.default_prdcode_map: dict = {'B': 'BO',
                                        'C': 'CNC',
                                        'F': 'MTF',
                                        'H': 'CO',
                                        'I': 'MIS',
                                        'M': 'NRML',
                                        'BO': 'BO',
                                        'CNC': 'CNC',
                                        'MTF': 'MTF',
                                        'CO': 'CO',
                                        'MIS': 'MIS',
                                        'NRML': 'NRML'}

    def _CheckUpdate(self):
        self.LOGGER.info("Checking for new update.")
        url = self.__router._CheckUpdateURl()
        rep = self.__http._PostMethod(url, json.dumps({"lib": "EAC_PYTHON", "vsn": self.__version}))
        self.LOGGER.debug("Response received: %s", rep)
        if not rep:
            return
        elif rep['data']['sts'] is True:
            if rep['data']['msg'] == 'OPTIONAL':
                self.LOGGER.info("New optional update found.")
                print("New version " + rep['data']['vsn'] + " is available. Stay up to date for better experience")
        elif rep['data']['sts'] is False:
            if rep['data']['msg'] == 'MANDATORY':
                print("Mandatory Update. New version " + rep['data']['vsn'] + '. Update to new version to continue.')
                self.LOGGER.info("Mandatory Update. New version " + rep['data']['vsn'] + '. Update to new version to continue.')
                sys.exit(0)

    def _setProductCodes(self) :
        self.__constants.ProductCodesMap : dict = self.__FetchProductCodes()

    def __FetchProductCodes(self) -> dict:
        exch_wise_product_codes = dict()
        
        session_data = json.loads(self.r.get(self.__constants.Filename))
        for prds in session_data['data']['data']['lgnData']['prds']:
            exc = prds['exc']
            for prd in prds['prd']:
                prdVal = prd['prdVal']
                if self.default_prdcode_map.get(prdVal) not in exch_wise_product_codes:
                        exch_wise_product_codes[self.default_prdcode_map.get(prdVal)] = {exc : prdVal}
                else :
                    exch_wise_product_codes[self.default_prdcode_map.get(prdVal)].update({exc : prdVal})
        return exch_wise_product_codes
    
    def getAlternateActionName(action: ActionEnum) -> str :
        
        if action.value == ActionEnum.BUY.value :
            return "B"
        elif action.value == ActionEnum.SELL.value :
            return "S"
        return ""