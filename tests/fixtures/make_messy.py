import random, datetime
from openpyxl import Workbook
random.seed(11)
H=["Name","Mobile","Alt_Mobile","Email","Batch_Name","College State","College","Mbbs_Batch","Company_options","Payment_mode","Status","Employee Name","Total_fee","Discount_fee","Paid_amount","Due_amount","Joining_date","Due_date","Attended_event_count","Attended_events"]
batches=["SSP2 (B) Achievers","SSP3 (A) Foundation","SSP1 (C) Rankers","OTO Mentorship 2026","FMGE June 2026"]
colleges=[("Government Medical College, Kadapa","Andhra Pradesh",14),("GMC Kadapa","Andhra Pradesh",8),("Govt. Medical College Kadappa","Andhra Pradesh",4),
("Fathima Institute of Medical Sciences, Kadapa","Andhra Pradesh",6),("Siddhartha Medical College, Vijayawada","Andhra Pradesh",12),("Siddhartha Medical College","Andhra Pradesh",5),("SMC Vijayawada","Andhra Pradesh",3),
("Narayana Medical College, Nellore","Andhra Pradesh",10),("Narayana Medical College","Andhra Pradesh",4),("Government Medical College, Ongole","Andhra Pradesh",7),
("Rangaraya Medical College, Kakinada","Andhra Pradesh",8),("Osmania Medical College","Telangana",12),("Gandhi Medical College, Secunderabad","Telangana",9),("Kakatiya Medical College","Telangana",6),("Andhra Medical College, Visakhapatnam","Andhra Pradesh",7)]
events=["Anatomy class","Physiology class","Biochemistry class","Micro revision","Pharmacology class","Pathology GT discussion","Derma class","PSM class","Medicine marathon","Surgery class","OBG class"]
first=["Aarav","Priya","Rahul","Sneha","Kiran","Divya","Arjun","Meera","Sai","Lakshmi","Vikram","Anjali","Rohit","Pooja","Teja","Harini"]
last=["Reddy","Rao","Sharma","Kumar","Naidu","Varma","Iyer","Patel"]
wb=Workbook(); ws=wb.active; ws.append(H); i=0
for c,s,n in colleges:
  for _ in range(n):
    i+=1; total=random.choice([45000,60000,75000,90000]); disc=random.choice([0,0,5000]); paid=random.choice([total-disc,(total-disc)//2,20000]); due=max(0,total-disc-paid); ev=random.sample(events,random.randint(0,5))
    jd=datetime.date(2025,6,1)+datetime.timedelta(days=random.randint(0,520))
    ws.append([f"Dr. {random.choice(first)} {random.choice(last)}",str(9100000000+i*7919),"",f"s{i}@example.com",random.choice(batches),s,c,str(random.choice([2019,2020,2021])),random.choice(["ASC Edtech","DBMCI Hyderabad"]),random.choice(["UPI","Cash","EMI"]),"Active" if due==0 else "Pending",random.choice(["Ravi","Sunitha","Mahesh"]),total,disc,paid,due,jd.isoformat(),"",len(ev),", ".join(ev)])
wb.save("tests/fixtures/messy.xlsx"); print(i)
