# NOTE: no openstack_cloud variable here - the provider is configured at the
# root module; child modules inherit it.

variable "instance_name" {
  type        = string
  description = "Instance name in the OpenStack catalog."
  default     = "garden"
}

variable "region" {
  type        = string
  description = "OVH Public Cloud region (e.g. GRA11, SBG5, BHS5)."
  default     = "" # TODO(you)
}

variable "flavor_name" {
  type        = string
  description = "Flavor matching 8 vCPU / 16 GB in the chosen region."
  default     = "" # TODO(you) + TODO(verify): check the region's catalog, do not guess
}

variable "image_name" {
  type        = string
  description = "Image to boot."
  default     = "Fedora 44" # TODO(verify): exact catalog name in the chosen region
}

variable "ssh_public_key" {
  type        = string
  description = "Admin SSH public key installed by cloud-init."
  default     = "" # TODO(you)
}

variable "admin_user" {
  type        = string
  description = "Admin user created by cloud-init."
  default     = "fedora"
}

variable "admin_cidr" {
  type        = string
  description = "Your current public IP as a /32 - the ONLY temporary SSH source. Emptied by the bootstrap close-out."
  default     = "" # TODO(you): curl -4 ifconfig.me
}

variable "volume_size_gb" {
  type        = number
  description = "Data volume size (runner work dirs, /srv)."
  default     = 200
}

variable "tailscale_udp_port" {
  type        = number
  description = "WireGuard/UDP port Tailscale listens on for direct connections."
  default     = 41641
}
