import logging

class OrderHelper:
    def __init__(self) -> None:
        self.LOGGER = logging.getLogger(__name__)
        self.LOGGER.info("OrderHelper object is being created.")

    def _CheckDependentAndUpdateData(self, data, accountData):  
        try:
            if 'empOrDependent' in accountData:
                data['empOrDependent'] = accountData['empOrDependent']
        except Exception as ex:
            self.LOGGER.error("Employee Or Dependent is not present")
            raise ex