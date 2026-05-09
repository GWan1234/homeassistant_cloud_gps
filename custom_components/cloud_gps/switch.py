"""switch Entities"""
import logging
import time
import datetime
import json
import requests
import asyncio
from async_timeout import timeout

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.components.switch import (
    SwitchEntity, 
    SwitchEntityDescription
)

from homeassistant.const import (
    CONF_USERNAME,
    CONF_PASSWORD,
)

from .const import (
    COORDINATOR,
    DOMAIN,
    CONF_WEB_HOST,
    CONF_SWITCHS,
    MQTT_MANAGER,
)


_LOGGER = logging.getLogger(__name__)


SWITCH_TYPES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="defence",
        name="defence",
        icon="mdi:shield"
    ),
    SwitchEntityDescription(
        key="open_lock",
        name="open_lock",
        icon="mdi:lock-open"
    ),
    SwitchEntityDescription(
        key="defencemode",
        name="defencemode",
        icon="mdi:lock-open"
    )
)

SWITCH_TYPES_MAP = { description.key: description for description in SWITCH_TYPES }

SWITCH_TYPES_KEYS = { description.key for description in SWITCH_TYPES }


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add Switchentities from a config_entry."""      
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    webhost = config_entry.data[CONF_WEB_HOST]
    username = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]
    enabled_switchs = [s for s in config_entry.options.get(CONF_SWITCHS, []) if s in SWITCH_TYPES_KEYS]
    
    for coordinatordata in coordinator.data:
        switchs = []
        for switch_type in enabled_switchs:
            mqtt_manager = hass.data[DOMAIN][config_entry.entry_id].get(MQTT_MANAGER)
            switchs.append(CloudGPSSwitchEntity(hass, webhost, username, password, coordinatordata, SWITCH_TYPES_MAP[switch_type], coordinator, mqtt_manager))
            
        async_add_entities(switchs, False)            
            

class CloudGPSSwitchEntity(SwitchEntity):
    """Define an switch entity."""
    _attr_has_entity_name = True
      
    def __init__(self, hass, webhost, username, password, imei, description, coordinator, mqtt_manager=None):
        """Initialize."""
        super().__init__()
        self.entity_description = description
        self._hass = hass
        self._webhost = webhost
        self._username = username
        self._password = password
        self._imei = imei        
        self.coordinator = coordinator
        self._unique_id = f"{self.coordinator.data[self._imei]['location_key']}-{description.key}"
        self._attr_translation_key = f"{self.entity_description.name}"
        
        self._is_on = None
        self._doing = False             # 标志位：是否正在执行动作和等待同步
        self._last_response = None      # 用于存储并显示请求结果
        
        if webhost == "tuqiang123.com":
            from .tuqiang123_data_fetcher import DataSwitch
        elif webhost == "hellobike.com":
            from .hellobike_data_fetcher import DataSwitch
        elif webhost == "gps_mqtt":
            from .gps_mqtt_data_fetcher import DataSwitch
        elif webhost == "niu.com":
            from .niu_data_fetcher import DataSwitch
        else:
            _LOGGER.error("配置的实体平台不支持，请不要启用此按钮实体！")
            return
        
        if webhost == "gps_mqtt":
            self._switch = DataSwitch(hass, username, password, imei, mqtt_manager)
        else:
            self._switch = DataSwitch(hass, username, password, imei)
        
    @property
    def unique_id(self):
        return self._unique_id
        
    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.data[self._imei]["location_key"])},
            "name": self._imei,
            "manufacturer": self._webhost,
            "entry_type": DeviceEntryType.SERVICE,
            "model": self.coordinator.data[self._imei]["deviceinfo"]["device_model"],
            "sw_version": self.coordinator.data[self._imei]["deviceinfo"]["sw_version"],
        }

    @property
    def should_poll(self):
        """Return the polling requirement of the entity."""
        return True

    @property
    def is_on(self):
        """Check if switch is on."""        
        return self._is_on
        
    @property
    def available(self):
        """Return the available."""
        attr_available = True if (self.coordinator.data.get(self._imei, {}).get("attrs", {}).get("onlinestatus", "") == "在线" ) else False
        if not attr_available:
            return False
            
        if self._webhost == "tuqiang123.com" and self.entity_description.key == "defence":
            defence_mode = self.coordinator.data.get(self._imei, {}).get("attrs", {}).get("defence_mode", "")
            if defence_mode == "自动":
                return False 
                
        return True
        
    @property
    def state_attributes(self): 
        attrs = {}
        if self.coordinator.data.get(self._imei):            
            attrs["querytime"] = self.coordinator.data[self._imei]["attrs"]["querytime"]
        
        # 将指令结果显示在前端属性中
        if self._last_response:
            attrs["last_command_response"] = self._last_response

        return attrs 

    async def async_turn_on(self, **kwargs):
        """Turn switch on."""        
        self._doing = True
        self._is_on = True
        self.async_write_ha_state()  # 第一步：乐观更新 UI，秒级响应，防止开关回弹
        
        attrs = dict(self.coordinator.data.get(self._imei, {}).get("attrs", {}))
        resp = None
        
        if self._webhost == "tuqiang123.com":
            resp = await self._switch._turn_on(self.entity_description.key, attrs)
        else:
            res = await self._switch._turn_on(self.entity_description.key)
            if res: resp = res
            
        if resp and isinstance(resp, dict):
            self._last_response = f"Code: {resp.get('code', '')}, Msg: {resp.get('msg', '')}"
            _LOGGER.info("设备 %s 下发开启 (%s) 指令结果: %s", self._imei, self.entity_description.key, self._last_response)

        # 第二步：给服务器 4 秒钟时间同步数据库状态
        await asyncio.sleep(4)
        
        # 第三步：解除锁定并强制 coordinator 拉取最新的数据
        self._doing = False
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn switch off."""        
        self._doing = True
        self._is_on = False
        self.async_write_ha_state()  # 第一步：乐观更新 UI，秒级响应，防止开关回弹
        
        attrs = dict(self.coordinator.data.get(self._imei, {}).get("attrs", {}))
        resp = None
        
        if self._webhost == "tuqiang123.com":
            resp = await self._switch._turn_off(self.entity_description.key, attrs)
        else:
            res = await self._switch._turn_off(self.entity_description.key)
            if res: resp = res
            
        if resp and isinstance(resp, dict):
            self._last_response = f"Code: {resp.get('code', '')}, Msg: {resp.get('msg', '')}"
            _LOGGER.info("设备 %s 下发关闭 (%s) 指令结果: %s", self._imei, self.entity_description.key, self._last_response)

        # 第二步：给服务器 4 秒钟时间同步数据库状态
        await asyncio.sleep(4)
        
        # 第三步：解除锁定并强制 coordinator 拉取最新的数据
        self._doing = False
        await self.coordinator.async_request_refresh()
        
    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self):
        """Update entity."""
        _LOGGER.debug("刷新switch数据")
        
        # 如果正在执行开/关动作及等待，跳过本次轮询，防止旧状态覆盖乐观更新的值
        if self._doing == False:
            if self._webhost == "hellobike.com":
                if self.entity_description.key == "defence":
                    self._is_on = self.coordinator.data[self._imei]["attrs"].get("defence")== "已设防"
                elif self.entity_description.key == "defencemode":
                    self._is_on = self.coordinator.data[self._imei]["attrs"].get("acc")== "已开锁"
                    
            elif self._webhost == "tuqiang123.com":
                if self.entity_description.key == "defencemode":
                    mode = self.coordinator.data[self._imei]["attrs"].get("defence_mode")
                    self._is_on = (mode == "自动")
                
                # open_lock 和 defence 在此处保持“无状态”，不进行状态判断
                    
            elif self._webhost == "gps_mqtt":
                if self.entity_description.key == "open_lock":
                    self._is_on = self.coordinator.data[self._imei]["attrs"].get("In1")== 1
            elif self._webhost == "niu.com":
                if self.entity_description.key == "open_lock":
                     self._is_on = self.coordinator.data[self._imei]["attrs"].get("acc") == "已开锁"