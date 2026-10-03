"""Small synthetic copies of every feed, in the real formats the parsers read."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

UN_XML = """<?xml version="1.0"?>
<CONSOLIDATED_LIST dateGenerated="2026-09-30T08:00:00.000Z">
 <INDIVIDUALS>
  <INDIVIDUAL>
   <DATAID>1</DATAID><REFERENCE_NUMBER>QDi.001</REFERENCE_NUMBER>
   <FIRST_NAME>MOHAMMAD</FIRST_NAME><SECOND_NAME>ALI</SECOND_NAME><THIRD_NAME>KHAN</THIRD_NAME>
   <UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE><LISTED_ON>2001-10-17</LISTED_ON>
   <COMMENTS1>Test entry &amp; remarks</COMMENTS1>
   <INDIVIDUAL_ALIAS><ALIAS_NAME>ALI KHAN BHAI</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_DATE_OF_BIRTH><DATE>1975-03-04</DATE></INDIVIDUAL_DATE_OF_BIRTH>
   <NATIONALITY><VALUE>Pakistan</VALUE></NATIONALITY>
  </INDIVIDUAL>
  <INDIVIDUAL>
   <DATAID>2</DATAID><REFERENCE_NUMBER>QDi.002</REFERENCE_NUMBER>
   <FIRST_NAME>JOHN</FIRST_NAME><SECOND_NAME>SMITH</SECOND_NAME>
   <UN_LIST_TYPE>Taliban</UN_LIST_TYPE><LISTED_ON>2005-01-01</LISTED_ON>
   <INDIVIDUAL_DATE_OF_BIRTH><YEAR>1960</YEAR></INDIVIDUAL_DATE_OF_BIRTH>
  </INDIVIDUAL>
 </INDIVIDUALS>
 <ENTITIES>
  <ENTITY><DATAID>3</DATAID><REFERENCE_NUMBER>QDe.003</REFERENCE_NUMBER>
   <FIRST_NAME>NORTHERN TRADING COMPANY</FIRST_NAME><UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE>
   <LISTED_ON>2010-05-05</LISTED_ON></ENTITY>
 </ENTITIES>
</CONSOLIDATED_LIST>"""

# OFAC SDN: ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage, GRT, Vess_flag, Vess_owner, Remarks
OFAC_SDN_CSV = (
    '1001,"ZULFIQAR, Hassan Raza","individual","SDGT","-0-","-0-","-0-","-0-","-0-","-0-","-0-",'
    '"DOB 12 Jan 1982; nationality Pakistan; Passport AB123"\r\n'
    '1002,"ACME SHIPPING, LTD","-0-","IRAN","-0-","-0-","-0-","-0-","-0-","-0-","-0-","-0-"\r\n'
    '\x1a'
)
OFAC_ALT_CSV = (
    '1001,5001,"aka","HASSAN ZULFIKAR","-0-"\r\n'
    '1001,5002,"aka","-0-","-0-"\r\n'
)
OFAC_CONS_CSV = (
    '2001,"PETROV, Ivan","individual","UKRAINE-EO13662","-0-","-0-","-0-","-0-","-0-","-0-","-0-","DOB 1970"\r\n'
)
OFAC_CONS_ALT_CSV = '2001,6001,"aka","IVAN PETROFF","-0-"\r\n'

UK_XML = """<?xml version="1.0"?>
<Designations><DateGenerated>2026-09-29</DateGenerated>
 <Designation>
  <UniqueID>UK0001</UniqueID><RegimeName>Global Human Rights</RegimeName>
  <IndividualEntityShip>Individual</IndividualEntityShip><DateDesignated>2021-03-01</DateDesignated>
  <Names>
   <Name><Name1>OSAMA</Name1><Name6>RAHMAN</Name6><NameType>Primary name</NameType></Name>
   <Name><Name1>USAMA</Name1><Name6>REHMAN</Name6><NameType>Alias</NameType></Name>
  </Names>
  <Nationalities><Nationality>Pakistan</Nationality></Nationalities>
  <DOB>1969-06-06</DOB>
  <UKStatementofReasons>Involved in test activity</UKStatementofReasons>
 </Designation>
</Designations>"""

FIA_PAGE_HTML = (
    '<html><a href="/files/redbook-2026.pdf" class="x"><span>FIA Red Book 2026</span></a>'
    '<a href="/files/other.pdf">Annual report</a></html>'
)

NEWS_RSS_CLEAR = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Weather update for Karachi</title><link>https://n.example/1</link><pubDate>Mon</pubDate>
<description>Sunny skies</description><source url="https://n.example">Dawn</source></item></channel></rss>"""

NEWS_RSS_HIT = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Bilal Ahmed Qureshi arrested in fraud case</title><link>https://n.example/2</link>
<pubDate>Tue, 29 Sep 2026</pubDate><description>&lt;p&gt;Police say Bilal Qureshi was arrested&lt;/p&gt;</description>
<source url="https://n.example">The News</source></item>
<item><title>Qureshi family celebrates festival</title><link>https://n.example/3</link><pubDate>Tue</pubDate>
<description>No crime here</description></item>
<item><title>Bilal Qureshi wins cricket match</title><link>https://n.example/4</link><pubDate>Tue</pubDate>
<description>Great game</description></item></channel></rss>"""


def make_redbook_pdf(people=None) -> bytes:
    """A text PDF in the Red Book layout the parser expects (one block per accused)."""
    people = people or [
        ("TARIQ MEHMOOD SATTAR ALIAS TARIQ BHATTI", "ABDUL SATTAR", "35202-1111111-1", "01-02-1980", "LAHORE", "FIR No(s) 12/2019 Section 420"),
        ("SAJID IQBAL (MWS/T)", "MUHAMMAD IQBAL", "42101-2222222-3", "15/06/1975", "KARACHI", "FIR No(s) 88/2020 Section 302"),
    ]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for name, father, cnic, dob, circle, fir in people:
        lines = [
            f"(ZONE: FIA {circle} ZONE CIRCLE: {circle} CIRCLE)",
            fir + " Section",
            f"Name of Accused {name}",
            f"Father/Husband Name {father}",
            f"CNIC {cnic}",
            f"Date of Birth {dob}",
        ]
        for ln in lines:
            c.drawString(30, y, ln)
            y -= 16
        y -= 14
    c.save()
    return buf.getvalue()


# NACTA Fourth Schedule export, in the column layout used on the NACTA portal and its mirrors
NACTA_CSV = (
    "S.No,Primary Title / Name,Father Name,CNIC / ID Number,District,Province\n"
    "1,Muhammad Shakir,Qabil Khan,3740565359881,HANGU,PUNJAB\n"
    "2,Aamir Bilal alias Babu Jhangvee,Muhammad Bilal,3640177467701,PAKPATTAN,Punjab\n"
    "3,Naseebullah,nill,5420396187581,KILLA ABDULLAH,BALOCHISTAN\n"
    "4,Akhtar Muhammad Khalil,Gul Roz,1111111111166,BANNU,KP\n"
)
