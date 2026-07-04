[out:json][timeout:180];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|living_street|busway|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"](30.277,120.092,30.329,120.183);
);
out geom;
