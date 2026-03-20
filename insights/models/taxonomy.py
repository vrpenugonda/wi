"""
Incident Classification Taxonomy

L1 = Category (e.g., Network, Software, Hardware)
L2 = Subcategory (e.g., VPN_RemoteAccess, ApplicationErrors)
L3 = Product/Issue (e.g., Client Errors, Crash)
L4 = Resolution Bucket (dynamically derived per subcategory)
"""

# L1 -> L2 -> L3 Taxonomy
INCIDENT_TAXONOMY = {
    "Hardware": {
        "DeviceFailure": ["Laptop", "Desktop", "Workstation", "Server", "Endpoint", "Tablet", "Mobile Device", "Other"],
        "PeripheralMalfunction": ["Printer", "Scanner", "Monitor", "Docking Station", "Keyboard", "Mouse", "Other"],
        "LostStolenDevice": ["Laptop", "Mobile Device", "Tablet", "Badge/Access Card", "Other"]
    },
    "Software": {
        "ApplicationErrors": ["Crash", "Error", "Hang", "Freeze", "AppStore Install Failure", "Other"],
        "OSProblems": ["Boot", "Startup", "Update", "Other"],
        "ConfigurationIssues": ["Registry", "Driver Issues", "Environment Variables", "Drive Mappings", "Access Denied", "Other"]
    },
    "Network": {
        "NetworkHardware": ["Router", "Switch", "Firewall", "Network Interface", "Cabling", "Load Balancer", "WAP/Controller", "Proxy", "Other"],
        "ConnectivityIssues": ["General", "Wi-Fi", "LAN/Wired", "Captive Portal", "ISP Degradation", "Hotspot", "Fixed Wireless", "Signal Strength", "LOA/M&A Network Issues", "Other"],
        "PerformanceIssues": ["Latency", "Bandwidth", "Packet Loss", "Jitter", "Other"],
        "DNS_IP_Problems": ["DNS Resolution", "DHCP", "IP Conflict", "Zone/Record Issues", "Other"],
        "VPN_RemoteAccess": ["Client Errors", "Authentication Failures", "Tunnel Drops", "Split Tunneling", "Profile/Policy", "Zscaler", "Cisco Secure", "Other"]
    },
    "WebApplications": {
        "Performance_Outage": ["Slow Response", "Timeouts", "Outage", "Other"],
        "UserInterface": ["Layout Issues", "Buttons/Links Not Working", "Forms", "Localization", "Accessibility", "Other"],
        "Data_API": ["API Errors", "Database Connection Issues", "Token/Auth Failures", "Rate Limiting", "Other"],
        "BrowserCompatibility": ["Unsupported Browser", "Cache", "Cookies", "Extensions", "Other"],
        "Security": ["SSL/TLS Certificate Errors", "Mixed Content", "CSP/Headers", "Other"],
        "HTTP_Errors": ["404", "403", "500", "502", "Unexpected Errors", "Other"]
    },
    "SecurityCompliance": {
        "SecurityIncidents": ["Unauthorized Access", "Data Breach", "Malware/Virus", "Phishing", "Ransomware", "Policy Violation", "Patch Management", "Lost/Stolen Device", "Other"],
        "Regulatory_Compliance": ["HIPAA/PHI", "Audit/Legal Hold", "Antitrust/CSI Handling", "Retention/Records (ERIM)", "Other"]
    },
    "PerformanceOptimization": {
        "SystemPerformance": ["Slow System", "High CPU/Memory Usage", "Storage Bottlenecks", "Thermal/Throttling", "Freezing/Locking", "Spinning/Waiting", "Disk Full", "Other"]
    },
    "UserAccessAuthentication": {
        "IdentityAccess": ["Password Reset", "Account Lockout", "MFA/SSO Issues", "Windows Hello", "Smart Card/Certificates", "Other"],
        "DirectoryServices": ["User Provisioning", "Group Management", "PKI/Certificate", "Federation", "Other"]
    },
    "CloudInfrastructure": {
        "Virtualization_Remote": ["Citrix Issues", "Virtual Desktop Issues", "Remote App", "Other"],
        "PublicCloud": ["AWS", "Azure", "GCP", "Network Enablement", "IAM/Policies", "Other"],
        "Containers_Kubernetes": ["Cluster Access", "Deployment Failures", "Autoscaling/HPA", "Ingress/Service", "Other"],
        "CI_CD_Pipelines": ["Build Failures", "Artifact Repositories", "Release Workflows", "Environment/Secrets", "Other"]
    },
    "DataStorage": {
        "Backup": ["Backup Failures", "Job Failures", "NetBackup/Veritas", "Other"],
        "Storage": ["Server Disk Space Shortage", "SAN/NAS Issues", "Infinidat", "Other"],
        "DatabaseIssues": ["Performance", "Connectivity", "Replication", "Backup/Restore", "Other"],
        "DataIntegrity": ["Data Corruption", "Schema/Format Issues", "ETL Transform Problems", "Other"]
    },
    "IntegrationDataMovement": {
        "FileTransfer": ["SFTP", "HTTPS", "ECG/MFT", "Validation/Nonrepudiation", "Other"],
        "Batch_Jobs_Scheduling": ["Job Failures", "Late/Skipped Runs", "Dependency Errors", "Scheduler/CRON", "Other"],
        "Messaging_Middleware": ["MQ", "Kafka", "API Gateway", "Queue Backlog", "Other"],
        "EDI_Transactions": ["270/271 Eligibility", "276/277 Claim Status", "278 Prior Auth", "820 Premium Payment", "834 Enrollment", "835 Remittance", "837 Professional", "837 Institutional", "837 Dental", "NCPDP", "Other"]
    },
    "MonitoringObservability": {
        "Logging": ["Agent/Collector Issues", "Ingestion Failures", "Parsing Errors", "Field Mapping Issues", "Other"],
        "APM_InfrastructureMonitoring": ["Alerting", "Dashboards", "Metric Gaps", "Dynatrace Managed/Cloud", "Other"],
        "Synthetic_Monitoring": ["Test Failures", "Coverage Gaps", "Threshold/Config Errors", "Other"]
    },
    "MainframeLegacy": {
        "zOS_Applications": ["Connectivity", "Batch", "Performance", "Other"],
        "LegacyPlatforms": ["AS/400", "COBOL", "Job Control", "Other"]
    },
    "HealthcareSystems": {
        "EHR_EMR": ["Epic", "eClinicalWorks", "Athena", "Care Conductor/ICUE", "Other"],
        "Clinical_Interoperability": ["HL7", "FHIR", "Interface Engine", "HIE/Gateway", "Other"],
        "Payer_Member_Provider": ["Member Portals", "Provider Portals", "Claims Adjudication", "Eligibility & Benefits", "Authorizations/Referrals", "Other"],
        "Quality_Programs": ["HEDIS", "Risk & Quality", "Care Management", "Other"]
    },
    "EmailCollaboration": {
        "Email": ["Outlook/Exchange", "Delivery Failures", "Spam/Phishing", "Retention/Compliance", "Group Mailbox", "Online Personal Archive", "Other"],
        "Collaboration": ["Teams", "SharePoint", "OneDrive", "OneDrive Sync", "Permissions", "Chat/Channels", "Other"]
    },
    "TelephonyContactCenter": {
        "UnifiedCommunications": ["Teams Voice", "SIP/Phones", "Voicemail", "Headset Configuration", "Other"],
        "ContactCenter": ["IVR", "Call Routing", "Agent Desktop", "Recording/Analytics", "Live Chat", "Other"]
    },
    "EndpointManagement": {
        "Enrollment_Compliance": ["MDM/Intune", "Policy Enforcement", "Compliance Failures", "Other"],
        "Patching_SoftwareDistribution": ["OS Patches", "App Deployments", "Driver/Firmware", "Other"],
        "Security_Baselines": ["AV/EDR", "Device Control", "Disk Encryption", "Other"]
    },
    "EndUserSupport": {
        "SupportRequests": ["User Training", "Service Requests", "Access Requests", "New Hire Setup", "Application Usage Questions", "Feature Requests", "How-To/Config Questions", "Other"]
    },
    "ProcessInformational": {
        "OperationalEvents": ["ISP Outage", "Power Outage", "Abandoned Calls", "Duplicate Incident", "Informational Ticket", "User Self-Resolved", "Documentation Request", "Change/Release Management", "Service Catalog/CMDB", "Other"]
    }
}


def get_all_categories() -> list[str]:
    """Get all L1 category names"""
    return list(INCIDENT_TAXONOMY.keys())


def get_subcategories(category: str) -> list[str]:
    """Get all L2 subcategories for a given category"""
    return list(INCIDENT_TAXONOMY.get(category, {}).keys())


def get_products(category: str, subcategory: str) -> list[str]:
    """Get all L3 products for a given category/subcategory"""
    return INCIDENT_TAXONOMY.get(category, {}).get(subcategory, [])


def get_flat_taxonomy() -> list[dict]:
    """Get flattened taxonomy as list of dicts"""
    result = []
    for category, subcategories in INCIDENT_TAXONOMY.items():
        for subcategory, products in subcategories.items():
            for product in products:
                result.append({
                    "category": category,
                    "subcategory": subcategory,
                    "product": product
                })
    return result
